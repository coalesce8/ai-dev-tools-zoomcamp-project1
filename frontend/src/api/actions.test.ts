import { ApiError } from "./client";
import { cancelJob, requeueJob } from "./actions";
import { jsonResponse } from "../test/http";

const job = { id: "j1", state: "QUEUED" };

describe("job actions", () => {
  let fetchMock: ReturnType<typeof vi.fn>;
  beforeEach(() => {
    fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
  });
  afterEach(() => vi.unstubAllGlobals());

  it.each([
    ["requeue", requeueJob],
    ["cancel", cancelJob],
  ] as const)("%s POSTs with no body, encodes the id and returns the job", async (name, fn) => {
    fetchMock.mockResolvedValue(jsonResponse(200, job));
    await expect(fn("a/b")).resolves.toEqual(job);
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe(`/api/v1/jobs/a%2Fb/${name}`);
    expect(init.method).toBe("POST");
    expect(init.body).toBeUndefined();
  });

  it.each([requeueJob, cancelJob])(
    "rejects a 409 with the server's code and message",
    async (fn) => {
      fetchMock.mockResolvedValue(
        jsonResponse(409, { error: { code: "conflict", message: "nope" } }),
      );
      const err = await fn("j1").catch((e: unknown) => e);
      expect(err).toBeInstanceOf(ApiError);
      expect(err).toMatchObject({
        status: 409,
        code: "conflict",
        message: "nope",
      });
    },
  );
});
