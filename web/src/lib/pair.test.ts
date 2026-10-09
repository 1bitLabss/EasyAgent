import { expect, it } from "vitest";
import { addressAfterPair, keepPairInAddress, manifestHref, readPairToken } from "@/lib/pair";

const href = "http://192.168.1.20:44721/?pair=abcDEF1234567890token";

it("reads the camera link", () => {
  expect(readPairToken(href)).toBe("abcDEF1234567890token");
  expect(readPairToken("http://127.0.0.1:44721/?token=desk-token-ok")).toBe("desk-token-ok");
});

it("leaves the pair link in the address on iPhone Safari", () => {
  expect(keepPairInAddress(true, false)).toBe(true);
  expect(addressAfterPair(href, true)).toBeNull();
});

it("clears the pair link once the home screen app is open, and on other browsers", () => {
  expect(addressAfterPair(href, keepPairInAddress(true, true))).toBe("/");
  expect(addressAfterPair(href, keepPairInAddress(false, false))).toBe("/");
});

it("points the home screen manifest at the same pair link", () => {
  expect(manifestHref("abcDEF1234567890token")).toBe("/manifest.webmanifest?pair=abcDEF1234567890token");
  expect(manifestHref("short")).toBeNull();
  expect(manifestHref("has space and more")).toBeNull();
});
