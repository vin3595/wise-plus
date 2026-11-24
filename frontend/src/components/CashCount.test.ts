import { describe, expect, it } from "vitest";
import { denomText, denomTotal } from "./CashCount";

describe("cash count", () => {
  it("adds a coin total to the notes", () => {
    expect(denomTotal({ n500: 2, n10: 1, coins: 137 })).toBe((1000 + 10 + 137) * 100);
  });
  it("describes the coin total last", () => {
    expect(denomText({ coins: 137, n500: 2, c10: 3 })).toBe("₹500 × 2, ₹10 coin × 3, Coins ₹137");
  });
});
