import { useEffect, useState, type FormEvent } from "react";
import type { PersonalDetails } from "../types";
import { EMPTY_PERSONAL_DETAILS, personalDetailsError } from "../lib/domain";
export default function PersonalDetailsSettings({value, onSave}: {
  value?: PersonalDetails; onSave(value: PersonalDetails): void;
}) {
  const [details, setDetails] = useState(value || EMPTY_PERSONAL_DETAILS);
  useEffect(() => {setDetails(value || EMPTY_PERSONAL_DETAILS);}, [value]);
  const [error, setError] = useState("");
  const [saved, setSaved] = useState(false);
  function update(key: keyof PersonalDetails, value: string | boolean) {
    setDetails(current => ({...current, [key]: value}));
    setSaved(false); setError("");
  }
  function submit(event: FormEvent) {
    event.preventDefault();
    const clean = Object.fromEntries(Object.entries(details).map(([key, value]) => [key, typeof value === "string" ? value.trim() : value])) as unknown as PersonalDetails;
    const issue = personalDetailsError(clean);
    if (issue) {setError(issue); return;}
    onSave(clean); setSaved(true);
  }
  return <section className="personal-settings">
    <h2>Your details</h2>
    <p className="sub spaced">Save the details you use often. They stay on this device and fill only matching placeholders.</p>
    <form onSubmit={submit} noValidate>
      <div className="personal-fields">
        {([
          ["name", "Full name", "text", "Your name"],
          ["email", "Email address", "email", "you@example.com"],
          ["phone", "Phone number", "tel", "+1 (310) 555-0148"],
          ["website", "Website", "text", "https://your-site.com"],
        ] as const).map(([key, label, type, placeholder]) => <label className="field" key={key}>{label}<input type={type} value={details[key]} maxLength={1000} placeholder={placeholder} onChange={e => update(key, e.target.value)} /></label>)}
        <label className="field personal-address">Address<textarea rows={2} value={details.address} maxLength={1000} placeholder="Street, city, postal code" onChange={e => update("address", e.target.value)} /></label>
      </div>
      <label className="option-row"><span>Automatically fill my details<small>Applies to unfilled fields in current and future drafts.</small></span><input type="checkbox" checked={details.autoFill} onChange={e => update("autoFill", e.target.checked)} /></label>
      <p className="sub spaced">Inserted details stay editable. Dates, times, amounts, and generic [LINK] fields stay for review. Your website fills [WEBSITE] or [PERSONAL WEBSITE]. Turning this off doesn’t remove details already inserted.</p>
      {error && <p className="error spaced" role="alert">{error}</p>}
      <div className="row spaced"><button type="submit">Save details</button>{saved && <span role="status" className="sub">Details saved</span>}</div>
    </form>
  </section>;
}
