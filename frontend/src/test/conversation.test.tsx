import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { Conversation, pinnedHeight, pinnedIndex } from "../App";

const L = (who: "user" | "agent", text: string, at: number) => ({ who, text, at }) as any;

describe("Conversation with a phone sheet open", () => {
  const lines = [L("agent", "Three coins look tradeable.", 1),
                 L("agent", "Top 10 wallets hold 36 percent, near your 40 percent limit. Still want it on the list?", 2)];

  it("keeps Vibe's latest line, even when she spoke after it", () => {
    expect(pinnedIndex(lines)).toBe(1);
    expect(pinnedIndex([...lines, L("user", "hmm", 3)])).toBe(1);
    expect(pinnedIndex([L("user", "hello", 1)])).toBe(0);
  });

  it("makes room for the whole line, at most three lines", () => {
    const lh = 26.1;                                   // t-lg 18px x lh-text 1.45
    expect(pinnedHeight(lh, 52.2)).toBe(53);           // two lines: all of it
    expect(pinnedHeight(lh, 78.3)).toBe(79);           // three lines: all of it
    expect(pinnedHeight(lh, 130)).toBe(79);            // five lines: three, the rest scrolls
  });

  it("marks only the pinned line and leaves the desktop list alone", () => {
    const withReply = [...lines, L("user", "hmm", 3)];
    const { container, rerender } = render(<div className="vt"><Conversation lines={withReply} asking connected configured pin /></div>);
    const ol = container.querySelector("ol.conv")!;
    expect(ol.classList.contains("pin")).toBe(true);
    const pinned = container.querySelectorAll("li.pinned");
    expect(pinned.length).toBe(1);
    expect(pinned[0].textContent).toContain("near your 40 percent limit");
    rerender(<div className="vt"><Conversation lines={withReply} asking connected configured /></div>);
    expect(container.querySelector("ol.conv")!.classList.contains("pin")).toBe(false);
    expect(container.querySelectorAll("li.pinned").length).toBe(0);
    expect((container.querySelector(".vt") as HTMLElement).style.getPropertyValue("--sheet-top")).toBe("");
  });
});
