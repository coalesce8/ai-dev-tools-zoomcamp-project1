import { cleanup, render, screen, waitFor } from "@testing-library/react";
import type { Mock } from "vitest";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import type { Job } from "../api/types";
import { jsonResponse, textResponse } from "../test/http";
import { SubmitJobForm } from "./SubmitJobForm";

function makeJob(over: Partial<Job> = {}): Job {
  return {
    id: "job-1",
    name: "nightly",
    spec: { duration_s: 30, fail_probability: 0, mode: "normal" },
    state: "QUEUED",
    max_attempts: 3,
    attempt_count: 0,
    created_at: "2026-10-26T10:00:00Z",
    started_at: null,
    finished_at: null,
    last_error: null,
    ...over,
  };
}

let fetchMock: ReturnType<typeof vi.fn>;
let onCreated: Mock<() => void>;

function posts() {
  return fetchMock.mock.calls.filter(
    (c) => (c[1] as RequestInit | undefined)?.method === "POST",
  );
}

function lastBody() {
  const calls = posts();
  return JSON.parse((calls[calls.length - 1][1] as RequestInit).body as string);
}

function setup(
  respond: () => Response | Promise<Response> = () =>
    jsonResponse(201, makeJob()),
) {
  fetchMock = vi.fn(() => Promise.resolve(respond()));
  vi.stubGlobal("fetch", fetchMock);
  onCreated = vi.fn<() => void>();
  render(
    <MemoryRouter>
      <SubmitJobForm onCreated={onCreated} />
    </MemoryRouter>,
  );
  return userEvent.setup();
}

const nameInput = () => screen.getByRole("textbox", { name: "Name" });
const durationInput = () =>
  screen.getByRole("textbox", { name: "Duration (s)" });
const probInput = () =>
  screen.getByRole("textbox", { name: "Fail probability" });
const attemptsInput = () =>
  screen.getByRole("textbox", { name: "Max attempts" });
const submitButton = () =>
  screen.getByRole("button", { name: /Submit job|Submitting…/ });

async function fill(
  user: ReturnType<typeof userEvent.setup>,
  input: HTMLElement,
  text: string,
) {
  await user.clear(input);
  if (text !== "") {
    await user.type(input, text, { skipAutoClose: true });
  }
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("SubmitJobForm structure", () => {
  it("has the named form, fields, select options and button", () => {
    setup();
    expect(screen.getByRole("form", { name: "Submit a job" })).toHaveAttribute(
      "novalidate",
    );
    const mode = screen.getByRole("combobox", { name: "Mode" });
    expect(
      Array.from(mode.querySelectorAll("option")).map((o) => o.value),
    ).toEqual(["normal", "always_fail"]);
    expect(submitButton()).toHaveTextContent("Submit job");
  });

  it("starts with the default values and sends nothing", () => {
    setup();
    expect(nameInput()).toHaveValue("");
    expect(durationInput()).toHaveValue("30");
    expect(probInput()).toHaveValue("0");
    expect(screen.getByRole("combobox", { name: "Mode" })).toHaveValue(
      "normal",
    );
    expect(attemptsInput()).toHaveValue("3");
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("uses text inputs with inputMode and no maxLength", () => {
    setup();
    for (const input of [
      nameInput(),
      durationInput(),
      probInput(),
      attemptsInput(),
    ]) {
      expect(input).toHaveAttribute("type", "text");
    }
    expect(durationInput()).toHaveAttribute("inputmode", "numeric");
    expect(attemptsInput()).toHaveAttribute("inputmode", "numeric");
    expect(probInput()).toHaveAttribute("inputmode", "decimal");
    expect(nameInput()).not.toHaveAttribute("maxlength");
  });
});

describe("SubmitJobForm submit", () => {
  it("sends one POST with the validated body", async () => {
    const user = setup();
    await user.type(nameInput(), "nightly");
    await user.click(submitButton());
    await waitFor(() => expect(onCreated).toHaveBeenCalledTimes(1));
    expect(posts()).toHaveLength(1);
    expect(posts()[0][0]).toBe("/api/v1/jobs");
    expect(lastBody()).toEqual({
      name: "nightly",
      spec: { duration_s: 30, fail_probability: 0, mode: "normal" },
      max_attempts: 3,
    });
  });

  it("sends the chosen values", async () => {
    const user = setup();
    await user.type(nameInput(), "x");
    await user.selectOptions(
      screen.getByRole("combobox", { name: "Mode" }),
      "always_fail",
    );
    await fill(user, durationInput(), "5");
    await fill(user, probInput(), "0.5");
    await fill(user, attemptsInput(), "1");
    await user.click(submitButton());
    await waitFor(() => expect(onCreated).toHaveBeenCalled());
    expect(lastBody()).toEqual({
      name: "x",
      spec: { duration_s: 5, fail_probability: 0.5, mode: "always_fail" },
      max_attempts: 1,
    });
  });

  it("submits on Enter in the name field", async () => {
    const user = setup();
    await user.type(nameInput(), "enter{Enter}");
    await waitFor(() => expect(onCreated).toHaveBeenCalledTimes(1));
    expect(posts()).toHaveLength(1);
    expect(lastBody().name).toBe("enter");
  });

  it("on success shows a status with a link, clears only the name, calls onCreated", async () => {
    const user = setup(() => jsonResponse(201, makeJob({ id: "a/b c" })));
    await user.type(nameInput(), "n");
    await fill(user, durationInput(), "5");
    await user.click(submitButton());
    const status = await screen.findByRole("status");
    expect(status).toHaveTextContent("Job submitted");
    expect(screen.getByRole("link")).toHaveAttribute("href", "/jobs/a%2Fb%20c");
    expect(nameInput()).toHaveValue("");
    expect(durationInput()).toHaveValue("5");
    expect(onCreated).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("disables the button while in flight and ignores a second submit", async () => {
    let resolve: (r: Response) => void = () => {};
    const user = setup(
      () => new Promise<Response>((r) => (resolve = r)) as unknown as Response,
    );
    await user.type(nameInput(), "slow");
    await user.click(submitButton());
    expect(submitButton()).toBeDisabled();
    expect(submitButton()).toHaveTextContent("Submitting…");
    await user.click(submitButton());
    await user.type(nameInput(), "{Enter}");
    expect(posts()).toHaveLength(1);
    expect(onCreated).not.toHaveBeenCalled();
    resolve(jsonResponse(201, makeJob()));
    await waitFor(() => expect(submitButton()).toHaveTextContent("Submit job"));
    expect(submitButton()).toBeEnabled();
    expect(onCreated).toHaveBeenCalledTimes(1);
  });

  it("renders the returned name as text only", async () => {
    const user = setup(() =>
      jsonResponse(201, makeJob({ name: "<img src=x onerror=alert(1)>" })),
    );
    await user.type(nameInput(), "<img src=x onerror=alert(1)>");
    await user.click(submitButton());
    await screen.findByRole("status");
    expect(document.querySelector("img")).toBeNull();
  });
});

describe("SubmitJobForm client validation (AC-14)", () => {
  const cases: [string, () => HTMLElement, string, string][] = [
    ["empty name", nameInput, "", "Name is required"],
    ["whitespace name", nameInput, "   ", "Name is required"],
    [
      "101-char name",
      nameInput,
      "a".repeat(101),
      "Name must be at most 100 characters",
    ],
    [
      "tab in name",
      nameInput,
      "a\tb",
      "Name must contain only printable characters",
    ],
    [
      "duration 0",
      durationInput,
      "0",
      "Duration must be between 1 and 600 seconds",
    ],
    [
      "duration 601",
      durationInput,
      "601",
      "Duration must be between 1 and 600 seconds",
    ],
    [
      "duration 30.5",
      durationInput,
      "30.5",
      "Duration must be a whole number of seconds",
    ],
    [
      "prob -0.01",
      probInput,
      "-0.01",
      "Fail probability must be between 0 and 1",
    ],
    [
      "prob 1.01",
      probInput,
      "1.01",
      "Fail probability must be between 0 and 1",
    ],
    ["prob abc", probInput, "abc", "Fail probability must be a number"],
    ["attempts 0", attemptsInput, "0", "Max attempts must be between 1 and 10"],
    [
      "attempts 11",
      attemptsInput,
      "11",
      "Max attempts must be between 1 and 10",
    ],
  ];

  it.each(cases)(
    "%s sends nothing and shows the message",
    async (_l, getInput, text, message) => {
      const user = setup();
      if (getInput !== nameInput) {
        await user.type(nameInput(), "ok");
      }
      const input = getInput();
      if (input === nameInput()) {
        if (text !== "") {
          // paste so tabs and long text go in as one change
          await user.click(input);
          await user.paste(text);
        }
      } else {
        await fill(user, input, text);
      }
      await user.click(submitButton());
      expect(posts()).toHaveLength(0);
      const msg = screen.getByText(message);
      expect(input).toHaveAttribute("aria-invalid", "true");
      expect(input.getAttribute("aria-describedby")).toBe(msg.id);
      expect(onCreated).not.toHaveBeenCalled();
    },
  );

  it("shows all messages and focuses the first invalid field", async () => {
    const user = setup();
    await fill(user, durationInput(), "0");
    await fill(user, attemptsInput(), "11");
    await user.click(submitButton());
    expect(screen.getByText("Name is required")).toBeInTheDocument();
    expect(
      screen.getByText("Duration must be between 1 and 600 seconds"),
    ).toBeInTheDocument();
    expect(
      screen.getByText("Max attempts must be between 1 and 10"),
    ).toBeInTheDocument();
    expect(nameInput()).toHaveFocus();
  });

  it("re-checks a field in error as it is edited, and only those", async () => {
    const user = setup();
    await user.click(submitButton());
    expect(screen.getByText("Name is required")).toBeInTheDocument();
    await fill(user, durationInput(), "abc");
    expect(
      screen.queryByText("Duration must be a whole number of seconds"),
    ).not.toBeInTheDocument();
    await user.type(nameInput(), "a");
    expect(screen.queryByText("Name is required")).not.toBeInTheDocument();
    expect(nameInput()).not.toHaveAttribute("aria-invalid");
  });
});

describe("SubmitJobForm server errors", () => {
  it("shows a 400 message verbatim and keeps the input", async () => {
    const msg = "body.name: Value error, name must be printable";
    const user = setup(() =>
      jsonResponse(400, { error: { code: "validation_error", message: msg } }),
    );
    await user.type(nameInput(), "keepme");
    await user.click(submitButton());
    expect(await screen.findByRole("alert")).toHaveTextContent(msg);
    expect(nameInput()).toHaveValue("keepme");
    expect(onCreated).not.toHaveBeenCalled();
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it.each([
    [
      "500 envelope",
      () =>
        jsonResponse(500, {
          error: { code: "internal_error", message: "boom" },
        }),
      "boom",
    ],
    [
      "network failure",
      () => Promise.reject(new TypeError("fail")) as unknown as Response,
      "Could not reach the server",
    ],
    [
      "502 html",
      () => textResponse(502, "<html>bad</html>", "text/html"),
      "Request failed with status 502",
    ],
  ])("%s", async (_l, respond, text) => {
    const user = setup(respond);
    await user.type(nameInput(), "n");
    await user.click(submitButton());
    expect(await screen.findByRole("alert")).toHaveTextContent(text);
    expect(screen.getByRole("alert").textContent).not.toMatch(
      /undefined|\[object/,
    );
    expect(onCreated).not.toHaveBeenCalled();
    expect(submitButton()).toBeEnabled();
  });

  it("clears the alert on the next submit and shows success after a failure", async () => {
    let fail = true;
    const user = setup(() =>
      fail
        ? jsonResponse(500, {
            error: { code: "internal_error", message: "boom" },
          })
        : jsonResponse(201, makeJob()),
    );
    await user.type(nameInput(), "n");
    await user.click(submitButton());
    await screen.findByRole("alert");
    fail = false;
    await user.click(submitButton());
    expect(await screen.findByRole("status")).toHaveTextContent(
      "Job submitted",
    );
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
});
