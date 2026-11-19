export async function gzip(text: string): Promise<ArrayBuffer> {
  const stream = new Blob([text]).stream().pipeThrough(new CompressionStream("gzip"));
  return new Response(stream).arrayBuffer();
}

/**
 * D1 returns BLOB columns as ArrayBuffer in production and as a byte array in
 * some local test/runtime adapters. Keep the conversion at the boundary so
 * callers do not have to guess which representation they received.
 */
export function blobBuffer(value: ArrayBuffer | ArrayBufferView | number[]): ArrayBuffer {
  if (value instanceof ArrayBuffer) return value;
  if (ArrayBuffer.isView(value)) {
    return value.buffer.slice(value.byteOffset, value.byteOffset + value.byteLength) as ArrayBuffer;
  }
  return Uint8Array.from(value).buffer;
}

export async function gunzip(bytes: ArrayBuffer): Promise<string> {
  const stream = new Blob([bytes]).stream().pipeThrough(new DecompressionStream("gzip"));
  return new Response(stream).text();
}
