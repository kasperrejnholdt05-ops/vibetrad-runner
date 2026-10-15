// Samme dekryptering som Vibetrad-siden bruger i browseren (WebCrypto + DecompressionStream).
// Brug: node tests/decrypt_webcrypto.mjs <envelope.json> <passphrase>
import { readFileSync } from "node:fs";

export async function decryptEnvelope(env, passphrase, subtle = globalThis.crypto.subtle) {
  const b = (s) => Uint8Array.from(atob(s), (c) => c.charCodeAt(0));
  const sortKeys = (o) => Object.keys(o).sort().reduce((a, k) => ((a[k] = o[k]), a), {});
  const base = await subtle.importKey("raw", new TextEncoder().encode(passphrase), "PBKDF2", false, ["deriveKey"]);
  const key = await subtle.deriveKey(
    { name: "PBKDF2", hash: "SHA-256", salt: b(env.salt), iterations: env.iter },
    base, { name: "AES-GCM", length: 256 }, false, ["decrypt"]);
  const aad = new TextEncoder().encode(JSON.stringify(sortKeys(env.public)));
  const plain = await subtle.decrypt({ name: "AES-GCM", iv: b(env.iv), additionalData: aad }, key, b(env.ct));
  const stream = new Blob([plain]).stream().pipeThrough(new DecompressionStream("gzip"));
  return JSON.parse(await new Response(stream).text());
}

const [, , file, pass] = process.argv;
if (file) {
  const out = await decryptEnvelope(JSON.parse(readFileSync(file, "utf8")), pass);
  process.stdout.write(JSON.stringify(out));
}
