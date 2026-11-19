import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { Sheet } from "../ui/Sheet";

describe("Sheet", () => {
  it("folds to its title when the handle is tapped, and opens again", () => {
    const onClose = vi.fn();
    render(<Sheet title="Coin 1" sheetKey="c1" onClose={onClose}><p>chart here</p></Sheet>);
    const grab = screen.getByRole("button", { name: "Fold down to the title" });
    expect(grab.getAttribute("aria-expanded")).toBe("true");
    fireEvent.click(grab);
    expect(screen.getByText("chart here").closest(".sheet-body")?.hasAttribute("hidden")).toBe(true);
    expect(screen.getByText("Coin 1")).toBeTruthy();
    const again = screen.getByRole("button", { name: "Show details" });
    expect(again.getAttribute("aria-expanded")).toBe("false");
    fireEvent.click(again);
    expect(screen.getByText("chart here").closest(".sheet-body")?.hasAttribute("hidden")).toBe(false);
    expect(onClose).not.toHaveBeenCalled();
  });
});
