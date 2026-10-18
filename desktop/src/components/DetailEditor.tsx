import { useState, type FormEvent } from "react";
import {
  CalendarBlankIcon,
  ClockIcon,
  LinkIcon,
  PhoneIcon,
  MapPinIcon,
  CurrencyDollarIcon,
  TextTIcon,
  CheckIcon,
} from "@phosphor-icons/react";
import type { Draft } from "../types";
import {
  draftDetails,
  draftParts,
  detailValueError,
  formatTime,
} from "../lib/domain";
import Modal from "./Modal";

const kinds = {
  "[DATE]": {
    label: "Date",
    Icon: CalendarBlankIcon,
    hint: "Choose the date you want to commit to.",
    example: "",
  },
  "[TIME]": {
    label: "Time",
    Icon: ClockIcon,
    hint: "Choose a time you’ve confirmed. Calendar availability isn’t connected.",
    example: "",
  },
  "[LINK]": {
    label: "Link",
    Icon: LinkIcon,
    hint: "Paste a meeting link, website, or email address. Use the exact value you want to share.",
    example: "https://meet.google.com/abc-defg-hij",
  },
  "[AMOUNT]": {
    label: "Amount",
    Icon: CurrencyDollarIcon,
    hint: "Include the currency or unit, and check the amount before inserting it.",
    example: "$125.00",
  },
  "[PHONE]": {
    label: "Phone number",
    Icon: PhoneIcon,
    hint: "Include a country code or extension if needed.",
    example: "+1 (310) 555-0148",
  },
  "[ADDRESS]": {
    label: "Address",
    Icon: MapPinIcon,
    hint: "Include the building, room, or other directions the recipient needs.",
    example: "123 Main Street, Suite 4\nLos Angeles, CA 90012",
  },
};
export function detailKind(token: string) {
  return (
    kinds[token as keyof typeof kinds] || {
      label: token.slice(1, -1).toLowerCase().replaceAll("_", " "),
      Icon: TextTIcon,
      hint: "Enter the detail you want to include. The model has left this for you to decide.",
      example: "Enter the missing detail",
    }
  );
}
export function DetailToken({
  token,
  value,
  onClick,
  disabled,
}: {
  token: string;
  value?: string;
  onClick(): void;
  disabled?: boolean;
}) {
  const { Icon, label } = detailKind(token);
  return (
    <button
      type="button"
      className={`time-token detail-token${value ? " filled" : ""}`}
      disabled={disabled}
      aria-label={
        value ? `Change ${label.toLowerCase()}: ${value}` : `Fill ${token}`
      }
      onClick={onClick}
    >
      <Icon size={14} />
      <span className="detail-token-label">{value || token}</span>
    </button>
  );
}
export default function DetailEditor({
  draft,
  detailKey,
  onClose,
  onSave,
}: {
  draft: Draft;
  detailKey: string;
  onClose(): void;
  onSave(value: string, input?: string): void;
}) {
  const detail = draftDetails(draft).find((item) => item.key === detailKey)!;
  const kind = detailKind(detail.token);
  const isDate = detail.token === "[DATE]",
    isTime = detail.token === "[TIME]";
  const [input, setInput] = useState(
    isDate || isTime ? detail.input || "" : detail.value || "",
  );
  const [error, setError] = useState("");
  let value = input.trim();
  try {
    if (isDate && input) {
      formatTime({ date: input, time: "12:00" });
      value = new Date(input + "T12:00:00").toLocaleDateString("en-US", {
        weekday: "long",
        month: "short",
        day: "numeric",
        year: "numeric",
      });
    }
    if (isTime && input) value = formatTime({ date: "", time: input });
  } catch {
    value = "";
  }
  const parts = draftParts(draft),
    index = parts.findIndex((part) => part.detail?.key === detailKey);
  const before = parts
    .slice(0, index)
    .map((part) => part.text)
    .join("");
  const after = parts
    .slice(index + 1)
    .map((part) => part.text)
    .join("");
  function submit(event: FormEvent) {
    event.preventDefault();
    const issue = detailValueError(detail.token, value);
    if (issue) {
      setError(issue);
      return;
    }
    try {
      onSave(value, isDate || isTime ? input : undefined);
    } catch (e) {
      setError((e as Error).message);
    }
  }
  return (
    <Modal
      title={`${detail.value ? "Edit" : "Fill in"} ${kind.label.toLowerCase()}`}
      onClose={onClose}
    >
      <form onSubmit={submit} noValidate>
        <p className="sub spaced">{kind.hint}</p>
        <label className="field">
          {kind.label}
          {detail.token === "[ADDRESS]" || !(detail.token in kinds) ? (
            <textarea
              data-initial-focus
              rows={3}
              maxLength={4000}
              value={input}
              aria-invalid={!!error}
              aria-describedby={error ? "detail-error" : undefined}
              onChange={(e) => {
                setInput(e.target.value);
                setError("");
              }}
              placeholder={kind.example}
            />
          ) : (
            <input
              data-initial-focus
              type={
                isDate
                  ? "date"
                  : isTime
                    ? "time"
                    : detail.token === "[PHONE]"
                      ? "tel"
                      : "text"
              }
              maxLength={4000}
              value={input}
              aria-invalid={!!error}
              aria-describedby={error ? "detail-error" : undefined}
              onChange={(e) => {
                setInput(e.target.value);
                setError("");
              }}
              placeholder={kind.example}
            />
          )}
        </label>
        {isTime && (
          <div className="row quick-times" aria-label="Quick time choices">
            {["09:00", "14:00", "16:30"].map((time) => (
              <button
                type="button"
                key={time}
                aria-pressed={input === time}
                onClick={() => {
                  setInput(time);
                  setError("");
                }}
              >
                {formatTime({ date: "", time })}
              </button>
            ))}
          </div>
        )}
        {(isDate || isTime) && (
          <p className="sub spaced">
            Local time · {Intl.DateTimeFormat().resolvedOptions().timeZone}
          </p>
        )}
        <div className="time-preview detail-preview">
          <span className="overline">In your draft</span>
          <p aria-live="polite">
            {before.length > 100 ? "…" : ""}
            {before.slice(-100)}
            <strong>{value || detail.token}</strong>
            {after.slice(0, 100)}
            {after.length > 100 ? "…" : ""}
          </p>
        </div>
        {error && (
          <p className="error" id="detail-error" role="alert">
            {error}
          </p>
        )}
        <div className="modal-footer">
          <button type="button" onClick={onClose}>
            Cancel
          </button>
          <button className="primary" type="submit" disabled={!value}>
            <CheckIcon size={16} />
            {detail.value ? "Save detail" : "Insert detail"}
          </button>
        </div>
      </form>
    </Modal>
  );
}
