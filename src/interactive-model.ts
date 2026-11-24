import {
  clampThinkingLevel, createProvider, getSupportedThinkingLevels, InMemoryCredentialStore,
  InMemoryModelsStore, type Api, type Model, type SimpleStreamOptions, type StreamFunction,
} from '@earendil-works/pi-ai';
import {ModelRuntime} from '@earendil-works/pi-coding-agent';
import {canonical, refuse, type Obj, type ProfileV1, type ProfileV2} from './contracts.js';

const zeroCost = {input: 0, output: 0, cacheRead: 0, cacheWrite: 0};

export async function createInteractiveModel(profile: ProfileV1 | ProfileV2, credential: string | undefined) {
  const runtime = await ModelRuntime.create({
    credentials: new InMemoryCredentialStore(), modelsStore: new InMemoryModelsStore(),
    modelsPath: null, refreshOnCreate: false, allowModelNetwork: false,
  });
  let model: Model<Api>;
  if (profile.schema === 'millforge-profile/v1') {
    const m = profile.model;
    model = {
      id: m.id, name: m.id, api: m.api, provider: 'millforge-selected', baseUrl: m.base_url,
      input: ['text'], cost: zeroCost, reasoning: false, contextWindow: m.context_tokens,
      maxTokens: m.max_output_tokens, compat: m.compat,
    } as Model<Api>;
  } else {
    const m = profile.model;
    const overrides = m.metadata.overrides;
    const catalog = m.metadata.source === 'pinned-catalog' ? runtime.getModel(m.provider, m.id) : undefined;
    if (m.metadata.source === 'pinned-catalog' && (!catalog || catalog.api !== m.api || catalog.baseUrl !== m.base_url)) {
      refuse('selected pinned catalog model is unavailable. Use millforge run for finite diagnostics.', 'UNSUPPORTED_CAPABILITY');
    }
    model = {
      ...catalog, ...overrides, id: m.id, api: m.api, provider: m.provider, baseUrl: m.base_url,
      cost: overrides.cost === null ? zeroCost : overrides.cost ?? catalog?.cost ?? zeroCost,
      compat: {...catalog?.compat, ...m.compat},
    } as Model<Api>;
    if (m.context_tokens > model.contextWindow || (m.max_output_tokens !== null && m.max_output_tokens > model.maxTokens)) {
      refuse('authored token budget exceeds selected capacity', 'UNSUPPORTED_CAPABILITY');
    }
  }
  const thinking = profile.schema === 'millforge-profile/v1' ? 'off' :
    profile.reasoning ?? clampThinkingLevel(model, 'medium');
  if (!getSupportedThinkingLevels(model).includes(thinking)) refuse('selected reasoning level is unsupported', 'UNSUPPORTED_CAPABILITY');

  // The launcher has already excluded OAuth and profile-v3. This route owns only one explicit
  // model and uses Pi's streaming implementation without the finite invocation adapter.
  if (model.api !== 'openai-completions' && model.api !== 'anthropic-messages') {
    refuse('unsupported TUI model API. Use millforge run for this profile.', 'UNSUPPORTED_CAPABILITY');
  }
  const openai = model.api === 'openai-completions';
  const native = openai ? await import('@earendil-works/pi-ai/api/openai-completions') :
    await import('@earendil-works/pi-ai/api/anthropic-messages');
  const nativeStream = native.streamSimple as StreamFunction<Api, SimpleStreamOptions>;
  const requestUrl = new URL(model.baseUrl.replace(/\/$/, '') + (openai ? '/chat/completions' : '/v1/messages?beta=true')).href;
  const fetchSelected: typeof globalThis.fetch = async (input, init) => {
    const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url;
    if (url !== requestUrl || init?.method?.toUpperCase() !== 'POST') refuse('unselected TUI model endpoint', 'INCOMPATIBLE_ROUTE');
    const headers = new Headers(init.headers);
    const admitted = credential === undefined ? !headers.has('authorization') && !headers.has('x-api-key') :
      openai ? headers.get('authorization') === `Bearer ${credential}` && !headers.has('x-api-key') :
        headers.get('x-api-key') === credential && !headers.has('authorization');
    if (!admitted) refuse('unselected TUI authentication route', 'AUTH_UNAVAILABLE');
    return globalThis.fetch(input, {...init, redirect: 'error'});
  };
  const stream: StreamFunction<Api, SimpleStreamOptions> = (selected, context, options) => {
    if (selected.api !== model.api || selected.id !== model.id || selected.provider !== model.provider ||
        selected.baseUrl !== model.baseUrl) refuse('unselected TUI model', 'INCOMPATIBLE_ROUTE');
    const maxTokens = profile.model.max_output_tokens;
    return nativeStream(model, context, {
      ...options, env: {}, fetch: fetchSelected, apiKey: credential ?? 'millforge-no-auth',
      ...(credential === undefined ? {headers: {authorization: null, 'x-api-key': null}} : {headers: {}}),
      ...(maxTokens === null ? {} : {maxTokens: Math.min(options?.maxTokens ?? maxTokens, maxTokens)}),
      ...(profile.sampling.temperature === null ? {} : {temperature: profile.sampling.temperature}),
      ...(profile.sampling.top_p === null ? {} : {samplingParams: {top_p: profile.sampling.top_p}}),
      ...(profile.schema === 'millforge-profile/v1' ? {cacheRetention: 'none'} :
        profile.cache ? {cacheRetention: profile.cache.retention} : {}),
    });
  };
  runtime.registerNativeProvider(createProvider({
    id: model.provider, name: 'Millforge selected route', baseUrl: model.baseUrl, models: [model],
    auth: {apiKey: {name: 'Selected Millforge launch credential', resolve: async () =>
      ({auth: {apiKey: credential ?? 'millforge-no-auth'}, source: 'explicit Millforge launch'})}},
    api: {stream, streamSimple: stream},
  }));
  const registered = runtime.getModel(model.provider, model.id);
  if (!registered || canonical(registered as unknown as Obj) !== canonical(model as unknown as Obj)) {
    refuse('TUI runtime model differs from selected profile', 'UNSUPPORTED_CAPABILITY');
  }
  return {runtime, model, thinking};
}
