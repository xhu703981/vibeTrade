import { describe, expect, it } from "vitest";
import { pct, signedPct, updatedAgo, usdF, usdSigned, big } from "../panels/format";
import { cardSentence } from "../ui/sheets";
import { VOICE_LABEL } from "../ui/VoiceLine";
import type { Card } from "../types";

describe("copy rules", () => {
  it("money has two decimals and a true minus; percents are signed with one decimal", () => {
    expect(usdF(5)).toBe("$5.00");
    expect(usdF(-0.1)).toBe("\u2212$0.10");
    expect(usdSigned(-1.234)).toBe("\u2212$1.23");
    expect(usdSigned(2)).toBe("+$2.00");
    expect(signedPct(12)).toBe("+12.0%");
    expect(signedPct(-2.54)).toBe("\u22122.5%");
    expect(pct(-3)).toBe("\u22123.0%");
    expect(big(900000)).toBe("900.0k");
  });
  it("staleness shows only after 30 seconds", () => {
    expect(updatedAgo(1000, 1020)).toBeNull();
    expect(updatedAgo(1000, 1000 + 180)).toBe("Updated 3 min ago");
  });
  it("the card reads as one sentence with the stop and the planned loss", () => {
    const c = { kind: "buy", coin_number: 1, amount_usdc: 0.41, stop_loss_pct: -24, loss_plan_usdc: 0.1 } as unknown as Card;
    expect(cardSentence(c)).toBe("Buy $0.41 of coin 1. Stop at \u221224%. Planned loss about $0.10.");
  });
  it("voice status labels", () => {
    expect(VOICE_LABEL.idle).toBe("Listening for \u2018Vibe\u2019");
    expect(VOICE_LABEL.speaking).toBe("In conversation");
    expect(VOICE_LABEL.working).toBe("Working on research");
  });
});
