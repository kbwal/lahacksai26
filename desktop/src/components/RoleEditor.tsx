import { useState, type FormEvent } from "react";
import { Layers } from "lucide-react";
import type { AppConnection, AppId, Persona } from "../types";
import Modal from "./Modal";
export default function RoleEditor({
  role,
  connections,
  onSave,
  onClose,
}: {
  role?: Persona;
  connections: AppConnection[];
  onSave(role: Persona): void;
  onClose(): void;
}) {
  const [name, setName] = useState(role?.name || "");
  const [description, setDescription] = useState(role?.description || "");
  const [excluded, setExcluded] = useState<AppId[]>(role?.excludedApps || []);
  function submit(e: FormEvent) {
    e.preventDefault();
    if (!name.trim() || !description.trim()) return;
    onSave({
      id: role?.id || crypto.randomUUID(),
      kind: role?.kind || "custom",
      name: name.trim(),
      description: description.trim(),
      excludedApps: role ? excluded : [],
      usableExamples: role?.usableExamples || 0,
      trainingReady: role?.trainingReady || false,
    });
  }
  return (
    <Modal title={role ? `Edit ${role.name}` : "Add a role"} onClose={onClose}>
      <form onSubmit={submit}>
        <label className="field">
          Role name
          <input
            autoFocus
            required
            maxLength={50}
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="e.g. Community organizer"
          />
        </label>
        <label className="field">
          When to use this voice
          <textarea
            required
            maxLength={300}
            rows={3}
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            placeholder="Organizing events and keeping volunteers updated"
          />
        </label>
        {role ? (
          <>
            <h3>App exceptions</h3>
            <p className="sub">
              Every app is allowed by default. Turn off an app to exclude this
              role.
            </p>
            {connections.map((app) => (
              <label className="option-row" key={app.id}>
                <span>
                  {app.name}
                  <small>
                    {app.connected
                      ? "Account connected"
                      : "Applies when connected"}
                  </small>
                </span>
                <input
                  type="checkbox"
                  aria-label={`Allow ${app.name} for this role`}
                  checked={!excluded.includes(app.id)}
                  onChange={(e) =>
                    setExcluded(
                      e.target.checked
                        ? excluded.filter((id) => id !== app.id)
                        : [...excluded, app.id],
                    )
                  }
                />
              </label>
            ))}
            <p className="sub spaced">
              Exclusions stop future drafts and future training from that app
              for this role. Existing drafts remain visible; trained style stays
              until retraining.
            </p>
          </>
        ) : (
          <div className="soft-panel">
            <span className="badge">
              <Layers size={15} />
              All apps included
            </span>
            <p className="sub spaced">
              Gmail, Slack, Outlook and future apps. You can add exceptions
              later. Training waits until there are enough usable examples.
            </p>
          </div>
        )}
        <div className="modal-footer">
          <button type="button" onClick={onClose}>
            Cancel
          </button>
          <button className="primary" type="submit">
            {role ? "Save role" : "Create role"}
          </button>
        </div>
      </form>
    </Modal>
  );
}
