const path = require('node:path');
const fs = require('node:fs/promises');
const { createWorkerClient } = require('./mail.cjs');
function registerCompose({ app, ipcMain, dialog, trusted, getWorkspace, getWindow, worker }) {
  const invoke = worker || createWorkerClient(app);
  let busy = false;
  const config = path.join(app.getPath('userData'), 'compose-model.json');
  async function chooseModel(changeModel) {
    let modelPath;
    if (!changeModel) {
      try { modelPath = JSON.parse(await fs.readFile(config, 'utf8')).path; } catch {}
    }
    if (!modelPath) {
      const root = path.resolve(__dirname, '../..');
      const chosen = await dialog.showOpenDialog(getWindow(), {
        title: 'Choose your local model',
        message: 'Select the Qwen3-4B-Base model folder. This stays on your Mac.',
        defaultPath: app.isPackaged ? app.getPath('documents') : path.join(root, 'models'),
        properties: ['openDirectory'],
      });
      if (chosen.canceled || !chosen.filePaths[0]) return null;
      modelPath = chosen.filePaths[0];
      await fs.access(path.join(modelPath, 'config.json'));
      await fs.mkdir(path.dirname(config), { recursive: true });
      await fs.writeFile(config, JSON.stringify({path:modelPath}), {mode:0o600});
    }
    return modelPath;
  }
  ipcMain.handle('compose:compare', async (event, request) => {
    trusted(event);
    if (busy) throw new Error('A message is already being drafted.');
    busy = true;
    try {
      if (!request || !['gmail','slack','discord','outlook'].includes(request.appId)) throw new Error('Choose an app.');
      if (typeof request.prompt !== 'string' || !request.prompt.trim() || request.prompt.length > 6000) throw new Error('Describe your message.');
      const workspace = await getWorkspace();
      const personas = workspace.roles.filter(p => p.id === request.personaId && !p.excludedApps.includes(request.appId));
      if (!personas.length) throw new Error('Choose a persona allowed for this app.');
      const modelPath = await chooseModel(request.changeModel);
      if (!modelPath) return null;
      const result = await invoke('desktop-compare', undefined, {
        prompt:request.prompt, recipient:'', subject:'', appId:request.appId,
        persona:'compare', personaId:'compare', description:'', personaIds:personas.map(p => p.id), modelPath,
      });
      if (!Array.isArray(result?.results)) throw new Error('Model returned an invalid comparison.');
      return result.results.map(r => ({
        id: r.id, missing: !!r.missing, body: typeof r.body === 'string' ? r.body.slice(0, 60000) : '',
        name: r.id === null ? 'No adapter' : (personas.find(p => p.id === r.id)?.name || r.id),
      }));
    } finally { busy = false; }
  });
  ipcMain.handle('compose:generate', async (event, request) => {
    trusted(event);
    if (busy) throw new Error('A message is already being drafted.');
    busy = true;
    try {
      if (!request || !['gmail','slack','discord','outlook'].includes(request.appId)) throw new Error('Choose an app.');
      for (const [key, max] of [['prompt',6000],['recipient',320],['subject',300]]) {
        if (typeof request[key] !== 'string' || request[key].length > max || /\0/.test(request[key])) throw new Error(`Invalid ${key}.`);
      }
      if (!request.prompt.trim()) throw new Error('Describe your message.');
      const workspace = await getWorkspace();
      const persona = workspace.roles.find(p => p.id === request.personaId);
      if (!persona || persona.excludedApps.includes(request.appId)) throw new Error('Choose a persona allowed for this app.');
      const modelPath = await chooseModel(request.changeModel);
      if (!modelPath) return null;
      const result = await invoke('desktop-compose', undefined, {
        prompt:request.prompt, recipient:request.recipient, subject:request.subject,
        appId:request.appId, persona:persona.name, personaId:persona.id, description:persona.description, modelPath,
      });
      if (typeof result?.body !== 'string' || !result.body.trim() || result.body.length > 60000) throw new Error('Model returned an invalid draft.');
      return {body:result.body};
    } finally { busy = false; }
  });
}
module.exports = { registerCompose };
