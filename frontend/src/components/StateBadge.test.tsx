import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { render, screen } from "@testing-library/react";
import { JOB_STATES } from "../api/types";
import StateBadge from "./StateBadge";

describe("StateBadge", () => {
  it.each(JOB_STATES)("renders %s as text with its classes", (state) => {
    render(<StateBadge state={state} />);
    const badge = screen.getByText(state, { exact: true });
    expect(badge).toHaveClass("state-badge", `state-${state.toLowerCase()}`);
  });

  it("gives each state its own background colour in the CSS", () => {
    const css = readFileSync(resolve(process.cwd(), "src/components/StateBadge.css"), "utf8");
    const colours = JOB_STATES.map((state) => {
      const rule = new RegExp(`\\.state-${state.toLowerCase()}\\s*\\{([^}]*)\\}`).exec(css);
      expect(rule, `rule for ${state}`).not.toBeNull();
      const bg = /background-color:\s*([^;]+);/.exec(rule![1]);
      expect(bg, `background for ${state}`).not.toBeNull();
      return bg![1].trim().toLowerCase();
    });
    expect(new Set(colours).size).toBe(JOB_STATES.length);
  });
});
