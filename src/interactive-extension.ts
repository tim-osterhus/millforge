import {CustomEditor, type ExtensionFactory} from '@earendil-works/pi-coding-agent';

const blocked = new Set([
  '/login', '/logout', '/trust', '/model', '/scoped-models', '/settings',
  '/resume', '/import', '/export', '/fork', '/clone', '/share', '/bug', '/reload',
]);

function quote(value: string): string {
  return "'" + value.replaceAll("'", "'\\''") + "'";
}

export function millforgeExtension(profileDir: string, profileLabel: string): ExtensionFactory {
  return pi => {
    pi.registerCommand('millforge', {
      description: 'Show Millforge session, profile, run and Harness² commands',
      getArgumentCompletions: prefix => ['profile', 'harness2'].filter(value => value.startsWith(prefix))
        .map(value => ({value, label: value})),
      handler: async (args, ctx) => {
        if (ctx.mode !== 'tui') return;
        const profile = [
          `Profile directory ${JSON.stringify(profileDir)}`,
          `Model ${ctx.model?.provider ?? ''}/${ctx.model?.id ?? ''} (${ctx.thinkingLevel ?? 'off'})`,
          `Tools ${pi.getActiveTools().join(', ') || 'none'}`,
          `millforge profile validate ${quote(profileDir)}`,
          'millforge inspect --run DIR',
        ];
        const harness = [
          'Harness²',
          'millforge optimize inspect --campaign DIR',
          'millforge optimize prepare --campaign campaign.json --output DIR --optimizer-bin ABS',
          'Obtain the campaign execution approval before running.',
          'millforge optimize run --campaign DIR --approval approval.json --optimizer-bin ABS',
          'millforge optimize assess --campaign DIR --evidence evidence.json --output ASSESSMENT',
          'Review the assessment. Obtain exact owner approval before adoption.',
          'millforge optimize adopt --campaign DIR --report report.json --approval adoption.json --destination DEST',
          'The CLI checks optimizer availability and compatibility on invocation.',
        ];
        if (args && args !== 'profile' && args !== 'harness2') {
          ctx.ui.notify('Use /millforge, /millforge profile or /millforge harness2.', 'warning');
          return;
        }
        ctx.ui.notify((args === 'profile' ? profile : args === 'harness2' ? harness : [...profile, '', ...harness]).join('\n'), 'info');
      },
    });

    pi.on('session_start', (_event, ctx) => {
      if (ctx.mode !== 'tui') return;
      ctx.ui.setStatus('millforge-profile', `Millforge in-memory session · ${profileLabel}`);
      // Keep Pi's native editor and key handlers. The public editor factory lets us guard
      // stock submit actions before Pi dispatches them, including completed or pasted text.
      ctx.ui.setEditorComponent((tui, theme, keys) => {
        const editor = new CustomEditor(tui, theme, keys, {embedWorkingStatus: true});
        let submit: ((text: string) => void) | undefined;
        Object.defineProperty(editor, 'onSubmit', {
          get: () => submit,
          set: (handler: ((text: string) => void) | undefined) => {
            submit = handler && (text => {
              const command = text.trim().split(/\s+/, 1)[0]!;
              if (blocked.has(command)) {
                ctx.ui.notify(`${command} is unavailable in a profile-scoped Millforge TUI.`, 'warning');
                editor.setText('');
                return;
              }
              handler(text);
            });
          },
        });
        return editor;
      });
    });
    pi.on('session_before_switch', event => event.reason === 'new' ? undefined : {cancel: true});
    pi.on('session_before_fork', () => ({cancel: true}));
  };
}
