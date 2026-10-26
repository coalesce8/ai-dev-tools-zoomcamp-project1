import { render, screen } from "@testing-library/react";
import { ApiError } from "../api/client";
import ErrorMessage from "./ErrorMessage";

describe("ErrorMessage", () => {
  it("renders an ApiError message in an alert", () => {
    render(<ErrorMessage error={new ApiError(409, "conflict", "Job is already terminal")} />);
    expect(screen.getByRole("alert")).toHaveTextContent("Job is already terminal");
  });

  it("renders the fallback message for other errors", () => {
    render(<ErrorMessage error={new Error("boom")} />);
    expect(screen.getByRole("alert")).toHaveTextContent("Something went wrong");
  });

  it.each([null, undefined])("renders nothing for %s", (error) => {
    const { container } = render(<ErrorMessage error={error} />);
    expect(container).toBeEmptyDOMElement();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
});
