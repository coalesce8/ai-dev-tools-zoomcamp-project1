import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import App from "./App";

function renderAt(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <App />
    </MemoryRouter>,
  );
}

describe("App routes", () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("renders the jobs page at /", () => {
    renderAt("/");
    expect(screen.getByRole("heading", { level: 1, name: "Jobs" })).toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("renders the job detail page with the id from the URL", () => {
    renderAt("/jobs/abc123");
    expect(screen.getByRole("heading", { level: 1, name: "Job" })).toBeInTheDocument();
    expect(screen.getByText("abc123")).toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("shows a URL-encoded id decoded", () => {
    renderAt("/jobs/a%20b");
    expect(screen.getByRole("heading", { level: 1, name: "Job" })).toBeInTheDocument();
    expect(screen.getByText("a b")).toBeInTheDocument();
  });

  it.each(["/nope", "/jobs", "/jobs/1/extra"])("renders not found at %s", (path) => {
    renderAt(path);
    expect(screen.getByText("Page not found")).toBeInTheDocument();
    const links = screen.getAllByRole("link");
    const back = links.find((link) => link.textContent !== "Runwatch");
    expect(back).toHaveAttribute("href", "/");
    expect(screen.queryByRole("heading", { level: 1 })).not.toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it.each(["/", "/jobs/abc", "/nope"])("shows the Runwatch layout linking to / at %s", (path) => {
    renderAt(path);
    expect(screen.getByRole("link", { name: "Runwatch" })).toHaveAttribute("href", "/");
  });
});
