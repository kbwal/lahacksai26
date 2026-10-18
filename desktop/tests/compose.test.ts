import { describe, it, expect, vi } from 'vitest';
import { createRequire } from 'node:module';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { sampleWorkspace } from '../src/lib/sample';
const { registerCompose } = createRequire(import.meta.url)('../electron/compose.cjs');
describe('local new-message generation boundary', () => {
  it('uses the saved persona, rejects excluded apps and never sends messages', async () => {
    const dir = await mkdtemp(path.join(tmpdir(), 'margin-compose-'));
    const handlers: Record<string, Function> = {};
    const workspace = sampleWorkspace();
    const worker = vi.fn().mockResolvedValue({body:'A new message with [LINK].'});
    const dialog = {showOpenDialog: vi.fn().mockResolvedValue({canceled:true,filePaths:[]})};
    registerCompose({app:{getPath:()=>dir},ipcMain:{handle:(key:string, fn:Function)=>handlers[key]=fn},dialog,trusted:vi.fn(),getWorkspace:async()=>workspace,getWindow:()=>null,worker});
    const request = {personaId:'professional',appId:'gmail',prompt:'Invite Alex',recipient:'Alex',subject:'Demo',changeModel:false};
    try {
      workspace.roles[0].excludedApps = ['gmail'];
      await expect(handlers['compose:generate']({},request)).rejects.toThrow('allowed');
      workspace.roles[0].excludedApps=[];
      await expect(handlers['compose:generate']({},request)).resolves.toBeNull();
      expect(worker).not.toHaveBeenCalled();
      const {writeFile}=await import('node:fs/promises');
      await writeFile(path.join(dir,'compose-model.json'),JSON.stringify({path:dir}));
      expect(await handlers['compose:generate']({},request)).toEqual({body:'A new message with [LINK].'});
      expect(worker).toHaveBeenCalledWith('desktop-compose',undefined,expect.objectContaining({persona:'Professional',description:workspace.roles[0].description,modelPath:dir}));
      await expect(handlers['compose:generate']({},{...request,prompt:'x'.repeat(6001)})).rejects.toThrow('Invalid prompt');
    } finally {await rm(dir,{recursive:true,force:true});}
  });
});
