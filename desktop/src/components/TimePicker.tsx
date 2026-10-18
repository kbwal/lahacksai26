import { useState, type FormEvent } from "react";
import { Check, Clock3, Globe } from "lucide-react";
import type { Draft, TimeChoice } from "../types";
import { formatTime } from "../lib/domain";
import Modal from "./Modal";
export default function TimePicker({
  draft,
  onInsert,
  onClose,
}: {
  draft: Draft;
  onInsert(choice: TimeChoice): void;
  onClose(): void;
}) {
  const [time, setTime] = useState(draft.timeEdit?.choice.time || "14:00"),
    [date, setDate] = useState(draft.timeEdit?.choice.date || ""),
    [error, setError] = useState("");
  let label = "";
  try {
    label = formatTime({ date, time });
  } catch {
    /* Invalid fields are handled at submission. */
  }
  function submit(e: FormEvent) {
    e.preventDefault();
    try {
      formatTime({ time, date });
      onInsert({ time, date });
    } catch (e) {
      setError((e as Error).message);
    }
  }
  return (
    <Modal
      title={draft.timeEdit ? "Change the time" : "Choose a time"}
      onClose={onClose}
    >
      <form onSubmit={submit}>
        <p className="sub spaced">
          {draft.timeEdit ? "Update the time" : "Replace [TIME]"} in your reply
          to {draft.recipient}.
        </p>
        <div className="time-fields">
          <label className="field">
            Date <small>· optional</small>
            <input
              type="date"
              value={date}
              onChange={(e) => setDate(e.target.value)}
            />
          </label>
          <label className="field">
            Time
            <input
              data-initial-focus
              type="time"
              required
              value={time}
              onChange={(e) => setTime(e.target.value)}
            />
          </label>
        </div>
        <div className="row quick-times" aria-label="Quick time choices">
          {["09:00", "14:00", "16:30"].map((value) => (
            <button
              key={value}
              type="button"
              aria-pressed={time === value}
              onClick={() => setTime(value)}
            >
              {formatTime({ date: "", time: value })}
            </button>
          ))}
        </div>
        <p className="sub row spaced">
          <Globe size={14} />
          Local time · {Intl.DateTimeFormat().resolvedOptions().timeZone}
        </p>
        <div className="time-preview">
          <span className="overline">In your draft</span>
          <p aria-live="polite">
            “<strong>{label || "Choose a time"}</strong> works for me.”
          </p>
        </div>
        <p className="sub">
          Choose a time you’ve confirmed. These choices don’t reflect calendar
          availability.
        </p>
        {error && (
          <p className="error" role="alert">
            {error}
          </p>
        )}
        <div className="modal-footer">
          <button type="button" onClick={onClose}>
            Cancel
          </button>
          <button type="submit" className="primary" disabled={!label}>
            <Check size={16} />
            {draft.timeEdit ? "Update time" : "Insert time"}
          </button>
        </div>
      </form>
    </Modal>
  );
}
export function TimeToken({
  onClick,
  label,
}: {
  onClick(): void;
  label?: string;
}) {
  return (
    <button
      type="button"
      className={`time-token${label ? " filled" : ""}`}
      aria-label={
        label ? `Change inserted time: ${label}` : "Choose a time for [TIME]"
      }
      onClick={onClick}
    >
      <Clock3 size={14} />
      {label || "[TIME]"}
    </button>
  );
}
