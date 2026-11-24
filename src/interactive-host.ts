import {mkdir, writeFile} from 'node:fs/promises';
import {dirname, join} from 'node:path';
import {
  createAgentSession, createAgentSessionRuntime, DefaultResourceLoader, initTheme,
  InteractiveMode, SessionManager, SettingsManager,
} from '@earendil-works/pi-coding-agent';
import {refuse} from './contracts.js';
import {publicError, type LoadedProfile} from './profile.js';
import {millforgeExtension} from './interactive-extension.js';
import {createInteractiveModel} from './interactive-model.js';

export interface InteractiveInput {
  root: string;
  loaded: LoadedProfile;
  credential?: string;
}

async function runInteractive({root, loaded, credential}: InteractiveInput): Promise<void> {
  const profile = loaded.profile;
  if ((profile.schema !== 'millforge-profile/v1' && profile.schema !== 'millforge-profile/v2') ||
      profile.mode !== 'agent' ||
      (profile.schema === 'millforge-profile/v2' && profile.model.auth.kind === 'oauth')) {
    refuse('unsupported interactive profile. Use millforge run for this profile.', 'UNSUPPORTED_CAPABILITY');
  }
  const cwd = process.cwd();
  const resourceRoot = join(root, 'resources');
  await mkdir(resourceRoot);
  const skillPaths: string[] = [];
  for (const asset of profile.assets) {
    if (asset.kind !== 'skill') continue;
    const target = join(resourceRoot, asset.path);
    await mkdir(dirname(target), {recursive: true});
    await writeFile(target, loaded.assets.get(asset.path)!, {flag: 'wx', mode: 0o600});
    if (target.endsWith('/SKILL.md')) skillPaths.push(target);
  }
  const agentsFiles = profile.assets.filter(asset => asset.kind === 'context').map(asset => ({
    path: join(loaded.dir, asset.path),
    content: loaded.assets.get(asset.path)!.toString('utf8'),
  }));
  await writeFile(join(root, 'keybindings.json'), JSON.stringify({
    'app.suspend': [], 'app.model.select': [], 'app.model.cycleForward': [], 'app.model.cycleBackward': [],
    'app.session.resume': [], 'app.session.fork': [],
  }), {flag: 'wx', mode: 0o600});

  const {runtime, model, thinking} = await createInteractiveModel(profile, credential);
  const settings = SettingsManager.inMemory({
    tuiMode: 'regular',
    cacheWarming: 'off', retry: {enabled: false, maxRetries: 0, provider: {maxRetries: 0}},
  }, {projectTrusted: false});
  const resourceLoader = new DefaultResourceLoader({
    cwd: resourceRoot, agentDir: root, settingsManager: settings,
    noExtensions: true, noSkills: true, noPromptTemplates: true, noThemes: true, noContextFiles: true,
    extensionFactories: [millforgeExtension(loaded.dir, `${profile.profile_id} r${profile.revision}`)],
    additionalSkillPaths: skillPaths,
    agentsFilesOverride: () => ({agentsFiles}),
    appendSystemPrompt: [], appendSystemPromptOverride: () => [profile.instructions],
  });
  await resourceLoader.reload();
  const extensions = resourceLoader.getExtensions();
  if (extensions.extensions.length !== 1 || extensions.errors.length) refuse('curated TUI extension could not be loaded');
  const runtimeHost = await createAgentSessionRuntime(async target => {
    if (target.cwd !== cwd || target.agentDir !== root || target.sessionManager.getSessionFile()) {
      refuse('interactive session must stay in memory and use the selected workspace');
    }
    const session = await createAgentSession({
      ...target, settingsManager: settings, resourceLoader, modelRuntime: runtime, model,
      thinkingLevel: thinking, scopedModels: [{model, thinkingLevel: thinking}], tools: profile.tools,
    });
    const active = session.session.getActiveToolNames();
    if (active.length !== profile.tools.length || profile.tools.some(tool => !active.includes(tool))) {
      await session.session.abort();
      session.session.dispose();
      refuse('interactive tool loadout differs from selected profile', 'UNSUPPORTED_CAPABILITY');
    }
    return {...session, services: {cwd, agentDir: root, modelRuntime: runtime, settingsManager: settings, resourceLoader, diagnostics: []}, diagnostics: []};
  }, {cwd, agentDir: root, sessionManager: SessionManager.inMemory(cwd)});
  initTheme(settings.getTheme(), false);
  const tui = new InteractiveMode(runtimeHost, {tuiMode: 'regular'});
  try {
    await tui.run();
  } finally {
    tui.stop();
    await runtimeHost.dispose();
  }
}

if (process.send) {
  process.on('SIGINT', () => process.kill(process.pid, 'SIGTERM'));
  process.once('message', (input: InteractiveInput) => {
    process.disconnect?.();
    void runInteractive(input).catch(error => {
      const diagnostic = publicError(error);
      process.stderr.write(JSON.stringify({code: diagnostic.code, message: diagnostic.message}) + '\n', () => process.exit(2));
    });
  });
}
