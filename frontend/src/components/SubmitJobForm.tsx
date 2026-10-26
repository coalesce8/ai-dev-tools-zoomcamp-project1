import { useId, useRef, useState } from "react";
import type { ChangeEvent, FormEvent } from "react";
import { Link } from "react-router";
import { createJob } from "../api/jobs";
import { INITIAL_JOB_FORM_VALUES, MODES, validateJobForm } from "../jobForm";
import type { JobFormErrors, JobFormField, JobFormValues } from "../jobForm";
import ErrorMessage from "./ErrorMessage";
import "./SubmitJobForm.css";

const FIELD_ORDER: JobFormField[] = [
  "name",
  "duration",
  "failProbability",
  "mode",
  "maxAttempts",
];

export function SubmitJobForm({ onCreated }: { onCreated: () => void }) {
  const idPrefix = useId();
  const [values, setValues] = useState<JobFormValues>(INITIAL_JOB_FORM_VALUES);
  const [errors, setErrors] = useState<JobFormErrors>({});
  const [submitting, setSubmitting] = useState(false);
  const [serverError, setServerError] = useState<unknown>(null);
  const [createdId, setCreatedId] = useState<string | null>(null);
  const inFlight = useRef(false);
  const refs = useRef<
    Partial<Record<JobFormField, HTMLInputElement | HTMLSelectElement | null>>
  >({});

  function onChange(field: JobFormField) {
    return (e: ChangeEvent<HTMLInputElement | HTMLSelectElement>) => {
      const next = { ...values, [field]: e.target.value };
      setValues(next);
      if (errors[field] !== undefined) {
        const result = validateJobForm(next);
        const message = result.ok ? undefined : result.errors[field];
        setErrors((prev) => {
          const copy = { ...prev };
          if (message === undefined) {
            delete copy[field];
          } else {
            copy[field] = message;
          }
          return copy;
        });
      }
    };
  }

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    if (inFlight.current) {
      return;
    }
    const result = validateJobForm(values);
    if (!result.ok) {
      setErrors(result.errors);
      setServerError(null);
      setCreatedId(null);
      const first = FIELD_ORDER.find((f) => result.errors[f] !== undefined);
      if (first) {
        refs.current[first]?.focus();
      }
      return;
    }
    setErrors({});
    setServerError(null);
    setCreatedId(null);
    inFlight.current = true;
    setSubmitting(true);
    try {
      const job = await createJob(result.body);
      setCreatedId(job.id);
      setValues((prev) => ({ ...prev, name: "" }));
      onCreated();
    } catch (err) {
      setServerError(err);
    } finally {
      inFlight.current = false;
      setSubmitting(false);
    }
  }

  function fieldProps(field: JobFormField) {
    const errorId = `${idPrefix}-${field}-error`;
    const invalid = errors[field] !== undefined;
    return {
      id: `${idPrefix}-${field}`,
      value: values[field],
      onChange: onChange(field),
      "aria-invalid": invalid ? (true as const) : undefined,
      "aria-describedby": invalid ? errorId : undefined,
      ref: (el: HTMLInputElement | HTMLSelectElement | null) => {
        refs.current[field] = el;
      },
    };
  }

  function fieldError(field: JobFormField) {
    const message = errors[field];
    if (message === undefined) {
      return null;
    }
    return (
      <p className="field-error" id={`${idPrefix}-${field}-error`}>
        {message}
      </p>
    );
  }

  return (
    <form
      className="submit-job-form"
      aria-label="Submit a job"
      noValidate
      onSubmit={onSubmit}
    >
      <div className="field">
        <label htmlFor={`${idPrefix}-name`}>Name</label>
        <input type="text" autoComplete="off" {...fieldProps("name")} />
        {fieldError("name")}
      </div>
      <div className="field">
        <label htmlFor={`${idPrefix}-duration`}>Duration (s)</label>
        <input
          type="text"
          inputMode="numeric"
          autoComplete="off"
          {...fieldProps("duration")}
        />
        {fieldError("duration")}
      </div>
      <div className="field">
        <label htmlFor={`${idPrefix}-failProbability`}>Fail probability</label>
        <input
          type="text"
          inputMode="decimal"
          autoComplete="off"
          {...fieldProps("failProbability")}
        />
        {fieldError("failProbability")}
      </div>
      <div className="field">
        <label htmlFor={`${idPrefix}-mode`}>Mode</label>
        <select {...fieldProps("mode")}>
          {MODES.map((m) => (
            <option key={m} value={m}>
              {m}
            </option>
          ))}
        </select>
        {fieldError("mode")}
      </div>
      <div className="field">
        <label htmlFor={`${idPrefix}-maxAttempts`}>Max attempts</label>
        <input
          type="text"
          inputMode="numeric"
          autoComplete="off"
          {...fieldProps("maxAttempts")}
        />
        {fieldError("maxAttempts")}
      </div>
      <ErrorMessage error={serverError} />
      {createdId !== null && (
        <p role="status">
          <span>Job submitted</span>{" "}
          <Link to={`/jobs/${encodeURIComponent(createdId)}`}>View job</Link>
        </p>
      )}
      <button type="submit" disabled={submitting}>
        {submitting ? "Submitting…" : "Submit job"}
      </button>
    </form>
  );
}

export default SubmitJobForm;
