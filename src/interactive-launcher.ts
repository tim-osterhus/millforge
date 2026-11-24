import {fork, spawnSync} from 'node:child_process';
import {mkdtemp, rm} from 'node:fs/promises';
import {constants, tmpdir} from 'node:os';
import {join} from 'node:path';
import {fileURLToPath} from 'node:url';
import {refuse, type ProfileV1, type ProfileV2} from './contracts.js';
import {loadProfile, resolveProfile, type SelectionEnv} from './profile.js';
import type {InteractiveInput} from './interactive-host.js';

export function shellQuote(value: string): string {
  return /^[A-Za-z0-9_./-]+$/.test(value) ? value : "'" + value.replaceAll("'", "'\\''") + "'";
}

function credentialName(profile: ProfileV1 | ProfileV2): string | null {
  if (profile.schema === 'millforge-profile/v1') return profile.model.credential_env;
  return profile.model.auth.kind === 'env' ? profile.model.auth.name : null;
}

export async function launchInteractive(
  explicitProfile: string | undefined,
  selection: SelectionEnv,
  writeError: (value: string) => void,
): Promise<number> {
  if (!process.stdin.isTTY || !process.stdout.isTTY) refuse('millforge tui requires terminal input and output');
  if (process.platform !== 'linux' && process.platform !== 'darwin') {
    refuse('millforge tui requires a Linux or macOS terminal. Use millforge run for finite invocations.');
  }

  let profileDir: string;
  try {
    profileDir = await resolveProfile(explicitProfile, selection);
  } catch (error) {
    if (explicitProfile !== undefined || (error as NodeJS.ErrnoException).code !== 'ENOENT') throw error;
    const example = fileURLToPath(new URL('../../../profiles/agent', import.meta.url));
    writeError(`Configure the installed agent example for your endpoint, then run:\n  millforge profile select ${shellQuote(example)}\nRelaunch with millforge or millforge tui.\n`);
    return 2;
  }
  const loaded = await loadProfile(profileDir);
  const profile = loaded.profile;
  if ((profile.schema !== 'millforge-profile/v1' && profile.schema !== 'millforge-profile/v2') ||
      profile.mode !== 'agent' ||
      (profile.schema === 'millforge-profile/v2' && profile.model.auth.kind === 'oauth')) {
    refuse('TUI requires an ordinary v1 or v2 agent profile with none or env authentication. Use millforge run for this profile.', 'UNSUPPORTED_CAPABILITY');
  }
  const name = credentialName(profile);
  const credential = name === null ? undefined : process.env[name];
  if (name !== null && !credential) refuse('named credential unavailable', 'AUTH_UNAVAILABLE');

  const stty = process.platform === 'darwin' ? '/bin/stty' : '/usr/bin/stty';
  const terminal = spawnSync(stty, ['-g'], {encoding: 'utf8', env: {}, stdio: ['inherit', 'pipe', 'ignore']});
  if (terminal.status !== 0 || !terminal.stdout.trim()) refuse('could not save terminal settings');
  const terminalState = terminal.stdout.trim();
  const root = await mkdtemp(join(tmpdir(), 'millforge-tui-'));
  const env: NodeJS.ProcessEnv = {
    HOME: root, XDG_CONFIG_HOME: root, XDG_CACHE_HOME: root, XDG_DATA_HOME: root,
    XDG_STATE_HOME: root, TMPDIR: root, PI_CODING_AGENT_DIR: root, PI_OFFLINE: '1',
  };
  for (const key of ['PATH', 'TERM', 'COLORTERM', 'LANG', 'LC_ALL', 'LC_CTYPE', 'NO_COLOR', 'TERM_PROGRAM']) {
    if (key !== name && process.env[key] !== undefined) env[key] = process.env[key];
  }

  try {
    // Inherit the real foreground terminal. IPC keeps the selected credential out of argv and the child/tool environment.
    const child = fork(fileURLToPath(new URL('./interactive-host.js', import.meta.url)), {
      env, execArgv: [], serialization: 'advanced', stdio: ['inherit', 'inherit', 'inherit', 'ipc'],
    });
    let requestedSignal: NodeJS.Signals | undefined;
    let shutdownTimer: NodeJS.Timeout | undefined;
    const signals: NodeJS.Signals[] = ['SIGINT', 'SIGTERM', 'SIGHUP'];
    const forward = (signal: NodeJS.Signals): void => {
      if (requestedSignal) return;
      requestedSignal = signal;
      child.kill(signal === 'SIGINT' ? 'SIGTERM' : signal);
      shutdownTimer = setTimeout(() => {
        if (child.kill('SIGKILL')) writeError('Interactive shutdown timed out. Tool descendant closure is unknown.\n');
      }, 5000).unref();
    };
    for (const signal of signals) process.on(signal, forward);
    try {
      const input: InteractiveInput = {root, loaded, ...(credential === undefined ? {} : {credential})};
      const exit = await new Promise<{code: number | null; signal: NodeJS.Signals | null}>((resolve, reject) => {
        let failure: Error | undefined;
        child.on('error', error => { failure = error; });
        child.once('close', (code, signal) => failure ? reject(failure) : resolve({code, signal}));
        child.send(input, error => {
          if (error && child.exitCode === null && child.signalCode === null) child.kill('SIGTERM');
        });
      });
      const signal = exit.signal ?? requestedSignal;
      if (exit.signal) {
        // Pi cannot write its restore sequences after an uncatchable signal.
        process.stdout.write('\x1b[?2004l\x1b[?1000l\x1b[?1002l\x1b[?1003l\x1b[?1006l\x1b[?1049l\x1b[?25h\x1b[0m\x1b[<u\x1b[>4;0m');
      }
      return signal ? 128 + constants.signals[signal] : exit.code ?? 2;
    } finally {
      clearTimeout(shutdownTimer);
      for (const signal of signals) process.off(signal, forward);
    }
  } finally {
    spawnSync(stty, [terminalState], {env: {}, stdio: ['inherit', 'ignore', 'ignore']});
    await rm(root, {recursive: true, force: true});
  }
}
