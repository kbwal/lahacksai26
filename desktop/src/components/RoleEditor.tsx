import { useId, useRef, useState, type FormEvent } from "react";
import { StackIcon as Layers } from "@phosphor-icons/react";
import type { AppConnection, AppId, Persona } from "../types";
import Modal from "./Modal";
import { roleFormErrors } from "../lib/domain";
export default function RoleEditor({
  role,
  connections,
  roles,
  onSave,
  onClose,
}: {
  role?: Persona;
  connections: AppConnection[];
  roles: Persona[];
  onSave(role: Persona): void;
  onClose(): void;
}) {
  const [name, setName] = useState(role?.name || "");
  const [description, setDescription] = useState(role?.description || "");
  const [excluded, setExcluded] = useState<AppId[]>(role?.excludedApps || []);
  const [errors, setErrors] = useState<{ name?: string; description?: string }>(
    {},
  );
  const nameInput = useRef<HTMLInputElement>(null),
    descriptionInput = useRef<HTMLTextAreaElement>(null),
    id = useId();
  function submit(e: FormEvent) {
    e.preventDefault();
    const validation = roleFormErrors(name, description, roles, role?.id);
    setErrors(validation);
    if (validation.name || validation.description) {
      (validation.name ? nameInput.current : descriptionInput.current)?.focus();
      return;
    }
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
    <Modal title={role ? `Edit ${role.name}` : "Add a persona"} onClose={onClose}>
      <form onSubmit={submit} noValidate>
        <label className="field">
          Persona name
          <input
            ref={nameInput}
            data-initial-focus
            required
            aria-invalid={!!errors.name}
            aria-describedby={errors.name ? `${id}-name-error` : undefined}
            maxLength={50}
            value={name}
            onChange={(e) => {
              setName(e.target.value);
              setErrors((current) => ({ ...current, name: undefined }));
            }}
            placeholder="e.g. Community organizer"
          />
        </label>
        {errors.name && (
          <p id={`${id}-name-error`} className="error" role="alert">
            {errors.name}
          </p>
        )}
        <label className="field">
          When to use this voice
          <textarea
            ref={descriptionInput}
            required
            aria-invalid={!!errors.description}
            aria-describedby={
              errors.description ? `${id}-description-error` : undefined
            }
            maxLength={300}
            rows={3}
            value={description}
            onChange={(e) => {
              setDescription(e.target.value);
              setErrors((current) => ({ ...current, description: undefined }));
            }}
            placeholder="Organizing events and keeping volunteers updated"
          />
        </label>
        {errors.description && (
          <p id={`${id}-description-error`} className="error" role="alert">
            {errors.description}
          </p>
        )}
        {role ? (
          <>
            <h3>App exceptions</h3>
            <p className="sub">
              Every app is allowed by default. Turn off an app to exclude this
              persona.
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
                  aria-label={`Allow ${app.name} for this persona`}
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
              for this persona. Existing drafts remain visible; trained style stays
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
              Gmail, Slack, Discord, Outlook and future apps. You can add exceptions
              later. Training waits until there are enough usable examples.
            </p>
          </div>
        )}
        <div className="modal-footer">
          <button type="button" onClick={onClose}>
            Cancel
          </button>
          <button className="primary" type="submit">
            {role ? "Save persona" : "Create persona"}
          </button>
        </div>
      </form>
    </Modal>
  );
}
