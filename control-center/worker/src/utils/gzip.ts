import {toArrayBuffer} from "../storage/sqlStore";

export async function gzip(text: string): Promise<ArrayBuffer> {
  const stream = new Blob([text]).stream().pipeThrough(new CompressionStream("gzip"));
  return new Response(stream).arrayBuffer();
}

/**
 * SQLite adapters may return BLOB columns as ArrayBuffer or a byte array.
 * Keep the conversion at the boundary so callers do not have to guess which
 * representation they received.
 */
export const blobBuffer = toArrayBuffer;

export async function gunzip(bytes: ArrayBuffer): Promise<string> {
  const stream = new Blob([bytes]).stream().pipeThrough(new DecompressionStream("gzip"));
  return new Response(stream).text();
}
