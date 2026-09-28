import { FormEvent, useId, useState } from "react";

import { api, identifier, Json, records, text } from "./api";

interface PartIdentifierLookupProps {
  fallback: string;
  onResolved: (part: Json) => void;
}

type MessageTone = "success" | "danger" | "info";

export function PartIdentifierLookup({ fallback, onResolved }: PartIdentifierLookupProps) {
  const inputId = `part-lookup-${useId().replace(/:/g, "")}`;
  const hintId = `${inputId}-hint`;
  const [matches, setMatches] = useState<Json[]>([]);
  const [message, setMessage] = useState("");
  const [tone, setTone] = useState<MessageTone>("info");
  const [loading, setLoading] = useState(false);

  function selectPart(part: Json) {
    setMatches([]);
    setTone("success");
    setMessage(`Selected ${text(part, "number")} — ${text(part, "name")}.`);
    onResolved(part);
  }

  async function lookup(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const entered = String(form.get("part_identifier") ?? "").trim();
    if (!entered) return;

    setLoading(true);
    setMatches([]);
    setMessage("");
    try {
      const params = new URLSearchParams({ identifier: entered, active: "true" });
      const result = await api<Json>(`/api/v1/inventory/parts/?${params}`);
      const parts = records(result, "parts");
      if (parts.length === 1) {
        selectPart(parts[0]);
      } else if (parts.length === 0) {
        setTone("danger");
        setMessage(`No part matches “${entered}”. ${fallback}`);
      } else {
        setTone("info");
        setMessage(`${parts.length} parts match “${entered}”. Choose the correct part.`);
        setMatches(parts);
      }
    } catch (error) {
      setTone("danger");
      setMessage(error instanceof Error ? error.message : "Part lookup failed.");
    } finally {
      setLoading(false);
    }
  }

  return <section className="part-lookup" aria-label="Part lookup">
    <form className="form-grid" role="search" aria-label="Part identifier lookup" onSubmit={(event) => void lookup(event)}>
      <label className="field" htmlFor={inputId}>
        <span className="field-label">Scan or enter part identifier<span aria-hidden="true"> *</span></span>
        <input
          id={inputId}
          name="part_identifier"
          type="text"
          autoComplete="off"
          autoCapitalize="characters"
          spellCheck="false"
          maxLength={120}
          required
          aria-describedby={hintId}
        />
        <small id={hintId}>Use a barcode or QR scanner, or enter a part, manufacturer, alternate, or vendor part number.</small>
      </label>
      <button className="button secondary" type="submit" disabled={loading}>
        {loading ? "Finding part…" : "Find part"}
      </button>
    </form>
    {message && <div className={`notice ${tone}`} role={tone === "danger" ? "alert" : "status"}>{message}</div>}
    {matches.length > 1 && <div className="record-list" role="group" aria-label="Matching parts">
      {matches.map((part) => <button className="link-button list-row" type="button" key={identifier(part)} onClick={() => selectPart(part)}>
        <span><strong>{text(part, "number")}</strong><small>{text(part, "name")}</small></span>
      </button>)}
    </div>}
  </section>;
}
