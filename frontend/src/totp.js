// Minimal RFC 6238 TOTP (SHA-1, 6 digits, 30s step) with a pure-JS SHA-1/HMAC
// (no Web Crypto) since `crypto.subtle` only works in a secure context
// (https, or http://localhost) and this app is often reached over plain
// http on a bare IP/hostname, where `crypto.subtle` is undefined.
// No server-side auth exists in this app, so each user's secret (see
// users.js) lives in localStorage — this gates the client-side login gate,
// not an API.

const BASE32_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";
const STEP_SECONDS = 30;
const DIGITS = 6;

export function generateSecret(byteLength = 20) {
  const bytes = crypto.getRandomValues(new Uint8Array(byteLength));
  return base32Encode(bytes);
}

function base32Encode(bytes) {
  let bits = 0;
  let value = 0;
  let output = "";
  for (const byte of bytes) {
    value = (value << 8) | byte;
    bits += 8;
    while (bits >= 5) {
      output += BASE32_ALPHABET[(value >>> (bits - 5)) & 31];
      bits -= 5;
    }
  }
  if (bits > 0) {
    output += BASE32_ALPHABET[(value << (5 - bits)) & 31];
  }
  return output;
}

function base32Decode(secret) {
  const clean = secret.toUpperCase().replace(/[^A-Z2-7]/g, "");
  let bits = 0;
  let value = 0;
  const bytes = [];
  for (const char of clean) {
    const idx = BASE32_ALPHABET.indexOf(char);
    if (idx === -1) continue;
    value = (value << 5) | idx;
    bits += 5;
    if (bits >= 8) {
      bytes.push((value >>> (bits - 8)) & 0xff);
      bits -= 8;
    }
  }
  return new Uint8Array(bytes);
}

function concatBytes(a, b) {
  const out = new Uint8Array(a.length + b.length);
  out.set(a, 0);
  out.set(b, a.length);
  return out;
}

function sha1(bytes) {
  const h0 = 0x67452301, h1 = 0xefcdab89, h2 = 0x98badcfe, h3 = 0x10325476, h4 = 0xc3d2e1f0;
  const bitLen = bytes.length * 8;
  const paddedLen = ((bytes.length + 8) >> 6 << 6) + 64;
  const padded = new Uint8Array(paddedLen);
  padded.set(bytes);
  padded[bytes.length] = 0x80;
  const view = new DataView(padded.buffer);
  view.setUint32(paddedLen - 4, bitLen >>> 0, false);
  view.setUint32(paddedLen - 8, Math.floor(bitLen / 0x100000000), false);

  let h = [h0, h1, h2, h3, h4];
  const w = new Uint32Array(80);
  for (let offset = 0; offset < paddedLen; offset += 64) {
    for (let i = 0; i < 16; i++) w[i] = view.getUint32(offset + i * 4, false);
    for (let i = 16; i < 80; i++) {
      const val = w[i - 3] ^ w[i - 8] ^ w[i - 14] ^ w[i - 16];
      w[i] = (val << 1) | (val >>> 31);
    }
    let [a, b, c, d, e] = h;
    for (let i = 0; i < 80; i++) {
      let f, k;
      if (i < 20) {
        f = (b & c) | (~b & d);
        k = 0x5a827999;
      } else if (i < 40) {
        f = b ^ c ^ d;
        k = 0x6ed9eba1;
      } else if (i < 60) {
        f = (b & c) | (b & d) | (c & d);
        k = 0x8f1bbcdc;
      } else {
        f = b ^ c ^ d;
        k = 0xca62c1d6;
      }
      const temp = ((a << 5) | (a >>> 27)) + f + e + k + w[i];
      e = d;
      d = c;
      c = (b << 30) | (b >>> 2);
      b = a;
      a = temp | 0;
    }
    h = [(h[0] + a) | 0, (h[1] + b) | 0, (h[2] + c) | 0, (h[3] + d) | 0, (h[4] + e) | 0];
  }

  const out = new Uint8Array(20);
  const outView = new DataView(out.buffer);
  for (let i = 0; i < 5; i++) outView.setUint32(i * 4, h[i] >>> 0, false);
  return out;
}

function hmacSha1(keyBytes, msgBytes) {
  const blockSize = 64;
  let key = keyBytes;
  if (key.length > blockSize) key = sha1(key);
  if (key.length < blockSize) {
    const padded = new Uint8Array(blockSize);
    padded.set(key);
    key = padded;
  }
  const oKeyPad = new Uint8Array(blockSize);
  const iKeyPad = new Uint8Array(blockSize);
  for (let i = 0; i < blockSize; i++) {
    oKeyPad[i] = key[i] ^ 0x5c;
    iKeyPad[i] = key[i] ^ 0x36;
  }
  const inner = sha1(concatBytes(iKeyPad, msgBytes));
  return sha1(concatBytes(oKeyPad, inner));
}

function counterToBytes(counter) {
  const buf = new ArrayBuffer(8);
  const view = new DataView(buf);
  // JS numbers are safe as a 32-bit counter for many centuries at a 30s step.
  view.setUint32(4, counter, false);
  return new Uint8Array(buf);
}

function totpAt(secret, counter) {
  const keyBytes = base32Decode(secret);
  const hmac = hmacSha1(keyBytes, counterToBytes(counter));
  const offset = hmac[hmac.length - 1] & 0x0f;
  const binCode =
    ((hmac[offset] & 0x7f) << 24) |
    ((hmac[offset + 1] & 0xff) << 16) |
    ((hmac[offset + 2] & 0xff) << 8) |
    (hmac[offset + 3] & 0xff);
  const code = binCode % 10 ** DIGITS;
  return code.toString().padStart(DIGITS, "0");
}

export function generateTOTP(secret, at = Date.now()) {
  const counter = Math.floor(at / 1000 / STEP_SECONDS);
  return totpAt(secret, counter);
}

// Allows +/- 1 step of clock drift between the browser and the authenticator app.
export function verifyTOTP(secret, token, at = Date.now()) {
  const cleanToken = token.trim();
  const counter = Math.floor(at / 1000 / STEP_SECONDS);
  for (let errorWindow = -1; errorWindow <= 1; errorWindow++) {
    if (totpAt(secret, counter + errorWindow) === cleanToken) return true;
  }
  return false;
}

export function buildOtpAuthUri(secret, { accountName, issuer = "Network Control Room" }) {
  const label = encodeURIComponent(`${issuer}:${accountName}`);
  const params = new URLSearchParams({
    secret,
    issuer,
    algorithm: "SHA1",
    digits: String(DIGITS),
    period: String(STEP_SECONDS),
  });
  return `otpauth://totp/${label}?${params.toString()}`;
}
