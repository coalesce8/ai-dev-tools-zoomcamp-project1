// Client-side checks that mirror POST /api/v1/jobs (#5). Pure TypeScript.
import type { JobCreate, WorkloadMode } from "./api/types";

export const NAME_MAX_LENGTH = 100;
export const DURATION_MIN_S = 1;
export const DURATION_MAX_S = 600;
export const MAX_ATTEMPTS_MIN = 1;
export const MAX_ATTEMPTS_MAX = 10;
export const MAX_ATTEMPTS_DEFAULT = 3;

export interface JobFormValues {
  name: string;
  duration: string;
  failProbability: string;
  mode: string;
  maxAttempts: string;
}

export type JobFormField = keyof JobFormValues;

export type JobFormErrors = Partial<Record<JobFormField, string>>;

export type JobFormResult =
  { ok: true; body: JobCreate } | { ok: false; errors: JobFormErrors };

export const INITIAL_JOB_FORM_VALUES: JobFormValues = {
  name: "",
  duration: "30",
  failProbability: "0",
  mode: "normal",
  maxAttempts: String(MAX_ATTEMPTS_DEFAULT),
};

export const MODES: readonly WorkloadMode[] = ["normal", "always_fail"];

// Python's str.isprintable(): everything except Other and Separator
// categories, with the ordinary space as the one allowed separator.
const NON_PRINTABLE = /[\p{Cc}\p{Cf}\p{Cs}\p{Co}\p{Cn}\p{Zl}\p{Zp}]/u;
const SPACE_SEPARATOR = /\p{Zs}/u;

function isPrintable(text: string): boolean {
  for (const ch of text) {
    if (NON_PRINTABLE.test(ch) || (ch !== " " && SPACE_SEPARATOR.test(ch))) {
      return false;
    }
  }
  return true;
}

function checkName(name: string): string | null {
  if (name.trim() === "") {
    return "Name is required";
  }
  if ([...name].length > NAME_MAX_LENGTH) {
    return `Name must be at most ${NAME_MAX_LENGTH} characters`;
  }
  if (!isPrintable(name)) {
    return "Name must contain only printable characters";
  }
  return null;
}

type Parsed =
  { value: number; error?: undefined } | { value?: undefined; error: string };

function parseInteger(
  raw: string,
  label: string,
  whole: string,
  min: number,
  max: number,
  unit: string,
): Parsed {
  const text = raw.trim();
  if (text === "") {
    return { error: `${label} is required` };
  }
  if (!/^-?\d+$/.test(text)) {
    return { error: `${label} must be ${whole}` };
  }
  const n = Number(text);
  if (n < min || n > max) {
    return { error: `${label} must be between ${min} and ${max}${unit}` };
  }
  return { value: n };
}

function parseProbability(raw: string): Parsed {
  const text = raw.trim();
  if (text === "") {
    return { error: "Fail probability is required" };
  }
  if (!/^-?(\d+(\.\d*)?|\.\d+)$/.test(text)) {
    return { error: "Fail probability must be a number" };
  }
  const n = Number(text);
  if (n < 0 || n > 1) {
    return { error: "Fail probability must be between 0 and 1" };
  }
  // -0 is accepted by the API; send a plain 0.
  return { value: n === 0 ? 0 : n };
}

export function validateJobForm(values: JobFormValues): JobFormResult {
  const errors: JobFormErrors = {};

  const nameError = checkName(values.name);
  if (nameError) {
    errors.name = nameError;
  }

  const duration = parseInteger(
    values.duration,
    "Duration",
    "a whole number of seconds",
    DURATION_MIN_S,
    DURATION_MAX_S,
    " seconds",
  );
  if (duration.error !== undefined) {
    errors.duration = duration.error;
  }

  const probability = parseProbability(values.failProbability);
  if (probability.error !== undefined) {
    errors.failProbability = probability.error;
  }

  const mode = MODES.find((m) => m === values.mode);
  if (mode === undefined) {
    errors.mode = "Mode must be normal or always_fail";
  }

  const attempts = parseInteger(
    values.maxAttempts,
    "Max attempts",
    "a whole number",
    MAX_ATTEMPTS_MIN,
    MAX_ATTEMPTS_MAX,
    "",
  );
  if (attempts.error !== undefined) {
    errors.maxAttempts = attempts.error;
  }

  if (
    Object.keys(errors).length > 0 ||
    duration.value === undefined ||
    probability.value === undefined ||
    mode === undefined ||
    attempts.value === undefined
  ) {
    return { ok: false, errors };
  }

  return {
    ok: true,
    body: {
      name: values.name,
      spec: {
        duration_s: duration.value,
        fail_probability: probability.value,
        mode,
      },
      max_attempts: attempts.value,
    },
  };
}
