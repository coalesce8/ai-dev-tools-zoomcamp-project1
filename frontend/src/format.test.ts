import { ageSeconds, formatRate, formatSeconds } from "./format";

describe("formatSeconds", () => {
  it.each([
    [0, "0s"],
    [45, "45s"],
    [59.9, "59s"],
    [60, "1m"],
    [125, "2m 5s"],
    [3600, "1h"],
    [3725, "1h 2m"],
    [90000, "1d 1h"],
    [-5, "0s"],
    [NaN, "—"],
  ])("formats %s as %s", (input, expected) => {
    expect(formatSeconds(input)).toBe(expected);
  });
});

describe("formatRate", () => {
  it.each([
    [null, "—"],
    [0, "0%"],
    [0.4, "40%"],
    [1, "100%"],
    [1 / 3, "33.3%"],
  ])("formats %s as %s", (input, expected) => {
    expect(formatRate(input)).toBe(expected);
  });
});

describe("ageSeconds", () => {
  const now = Date.parse("2026-10-26T10:02:05Z");

  it("returns seconds since the timestamp", () => {
    expect(ageSeconds("2026-10-26T10:00:00Z", now)).toBe(125);
  });

  it("treats Z and +00:00 the same", () => {
    expect(ageSeconds("2026-10-26T10:00:00+00:00", now)).toBe(
      ageSeconds("2026-10-26T10:00:00Z", now),
    );
  });

  it("clamps a future timestamp at 0", () => {
    expect(ageSeconds("2026-10-26T11:00:00Z", now)).toBe(0);
  });

  it("gives NaN for an unparseable timestamp, shown as a dash", () => {
    const age = ageSeconds("not a date", now);
    expect(age).toBeNaN();
    expect(formatSeconds(age)).toBe("—");
  });
});
