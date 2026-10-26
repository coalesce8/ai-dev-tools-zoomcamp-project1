import {
  DURATION_MAX_S,
  DURATION_MIN_S,
  INITIAL_JOB_FORM_VALUES,
  MAX_ATTEMPTS_DEFAULT,
  MAX_ATTEMPTS_MAX,
  MAX_ATTEMPTS_MIN,
  NAME_MAX_LENGTH,
  validateJobForm,
} from "./jobForm";
import type { JobFormField, JobFormValues } from "./jobForm";

const VALID: JobFormValues = {
  name: "nightly-sim",
  duration: "30",
  failProbability: "0",
  mode: "normal",
  maxAttempts: "3",
};

function run(over: Partial<JobFormValues>) {
  return validateJobForm({ ...VALID, ...over });
}

function errorOf(field: JobFormField, value: string) {
  const r = run({ [field]: value });
  return r.ok ? null : (r.errors[field] ?? null);
}

function bodyOf(over: Partial<JobFormValues>) {
  const r = run(over);
  if (!r.ok) {
    throw new Error(`unexpected errors ${JSON.stringify(r.errors)}`);
  }
  return r.body;
}

describe("constants", () => {
  it("match the API limits", () => {
    expect([
      NAME_MAX_LENGTH,
      DURATION_MIN_S,
      DURATION_MAX_S,
      MAX_ATTEMPTS_MIN,
      MAX_ATTEMPTS_MAX,
      MAX_ATTEMPTS_DEFAULT,
    ]).toEqual([100, 1, 600, 1, 10, 3]);
  });

  it("initial values are the defaults", () => {
    expect(INITIAL_JOB_FORM_VALUES).toEqual({
      name: "",
      duration: "30",
      failProbability: "0",
      mode: "normal",
      maxAttempts: "3",
    });
  });
});

describe("validateJobForm", () => {
  it("gives the exact body for valid values", () => {
    expect(run({})).toEqual({
      ok: true,
      body: {
        name: "nightly-sim",
        spec: { duration_s: 30, fail_probability: 0, mode: "normal" },
        max_attempts: 3,
      },
    });
  });

  it.each([
    ["1 character", "a"],
    ["100 characters", "a".repeat(100)],
    ["not trimmed", " a b "],
    ["non-ASCII and emoji", "Modèle-🌊"],
    ["100 emoji", "🌊".repeat(100)],
  ])("accepts name: %s, unchanged", (_label, name) => {
    expect(bodyOf({ name }).name).toBe(name);
  });

  it.each([
    ["", "Name is required"],
    ["   ", "Name is required"],
    ["a".repeat(101), "Name must be at most 100 characters"],
    ["🌊".repeat(101), "Name must be at most 100 characters"],
    ["a\nb", "Name must contain only printable characters"],
    ["a\tb", "Name must contain only printable characters"],
    ["a\u0000b", "Name must contain only printable characters"],
    ["a​b", "Name must contain only printable characters"],
    ["a b", "Name must contain only printable characters"],
    ["a b", "Name must contain only printable characters"],
  ])("rejects name %j", (name, message) => {
    expect(errorOf("name", name)).toBe(message);
  });

  it.each([
    ["1", 1],
    ["600", 600],
    [" 30 ", 30],
    ["007", 7],
  ])("accepts duration %j", (duration, expected) => {
    expect(bodyOf({ duration }).spec.duration_s).toBe(expected);
  });

  it.each([
    ["", "Duration is required"],
    ["  ", "Duration is required"],
    ["30.5", "Duration must be a whole number of seconds"],
    ["30.0", "Duration must be a whole number of seconds"],
    ["1e2", "Duration must be a whole number of seconds"],
    ["abc", "Duration must be a whole number of seconds"],
    ["3 0", "Duration must be a whole number of seconds"],
    ["0x1E", "Duration must be a whole number of seconds"],
    ["0", "Duration must be between 1 and 600 seconds"],
    ["601", "Duration must be between 1 and 600 seconds"],
    ["-1", "Duration must be between 1 and 600 seconds"],
    ["99999999999999999999", "Duration must be between 1 and 600 seconds"],
  ])("rejects duration %j", (duration, message) => {
    expect(errorOf("duration", duration)).toBe(message);
  });

  it.each([
    ["0", 0],
    ["1", 1],
    ["0.5", 0.5],
    [".5", 0.5],
    ["1.0", 1],
    ["0.25", 0.25],
    ["-0", 0],
    ["-0.0", 0],
  ])("accepts fail probability %j", (failProbability, expected) => {
    const sent = bodyOf({ failProbability }).spec.fail_probability;
    expect(sent).toBe(expected);
    expect(Object.is(sent, -0)).toBe(false);
  });

  it.each([
    ["", "Fail probability is required"],
    ["abc", "Fail probability must be a number"],
    ["0,5", "Fail probability must be a number"],
    ["NaN", "Fail probability must be a number"],
    ["Infinity", "Fail probability must be a number"],
    ["1e-1", "Fail probability must be a number"],
    ["0.5.1", "Fail probability must be a number"],
    ["-0.01", "Fail probability must be between 0 and 1"],
    ["1.01", "Fail probability must be between 0 and 1"],
    ["2", "Fail probability must be between 0 and 1"],
  ])("rejects fail probability %j", (failProbability, message) => {
    expect(errorOf("failProbability", failProbability)).toBe(message);
  });

  it.each(["normal", "always_fail"])("accepts mode %s", (mode) => {
    expect(bodyOf({ mode }).spec.mode).toBe(mode);
  });

  it.each(["bogus", "NORMAL", ""])("rejects mode %j", (mode) => {
    expect(errorOf("mode", mode)).toBe("Mode must be normal or always_fail");
  });

  it.each([
    ["1", 1],
    ["10", 10],
  ])("accepts max attempts %j", (maxAttempts, expected) => {
    expect(bodyOf({ maxAttempts }).max_attempts).toBe(expected);
  });

  it.each([
    ["", "Max attempts is required"],
    ["2.5", "Max attempts must be a whole number"],
    ["abc", "Max attempts must be a whole number"],
    ["0", "Max attempts must be between 1 and 10"],
    ["11", "Max attempts must be between 1 and 10"],
  ])("rejects max attempts %j", (maxAttempts, message) => {
    expect(errorOf("maxAttempts", maxAttempts)).toBe(message);
  });

  it("reports every invalid field at once", () => {
    const r = validateJobForm({
      name: "",
      duration: "0",
      failProbability: "abc",
      mode: "bogus",
      maxAttempts: "11",
    });
    expect(r).toEqual({
      ok: false,
      errors: {
        name: "Name is required",
        duration: "Duration must be between 1 and 600 seconds",
        failProbability: "Fail probability must be a number",
        mode: "Mode must be normal or always_fail",
        maxAttempts: "Max attempts must be between 1 and 10",
      },
    });
  });
});
