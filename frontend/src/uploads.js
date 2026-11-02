// Both files and pasted text use Blobs, so cloud uploads never need a base64 copy.
export const MAX_DOCUMENT_BYTES = 20_000_000;

async function base64(blob) {
  const bytes = new Uint8Array(await blob.arrayBuffer());
  let binary = "";
  for (let offset = 0; offset < bytes.length; offset += 8192) {
    binary += String.fromCharCode(...bytes.subarray(offset, offset + 8192));
  }
  return btoa(binary);
}

export async function uploadDocument(transport, id, name, blob) {
  if (!(blob instanceof Blob)) throw new Error("Choose a file or paste text to upload.");
  if (blob.size > MAX_DOCUMENT_BYTES) throw new Error("Files must be under 20 MB.");
  if (!transport.directUploads) {
    return transport.call(`/api/knowledge/${id}/documents`, {
      method: "POST", body: JSON.stringify({ name, data: await base64(blob) }),
    });
  }
  const digest = await crypto.subtle.digest("SHA-256", await blob.arrayBuffer());
  const sha256 = Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
  const ticket = await transport.call(`/api/knowledge/${id}/uploads`, {
    method: "POST", body: JSON.stringify({ name, size: blob.size, sha256 }),
  });
  await transport.uploadForm(ticket.url, ticket.fields, blob, name);
  const complete = () => transport.call(`/api/knowledge/${id}/uploads/${encodeURIComponent(ticket.upload_id)}/complete`, {
    method: "POST", body: "{}",
  });
  try { return await complete(); }
  catch (error) {
    // Completion is idempotent. A lost response must not re-upload the file or register a second document.
    if (error instanceof TypeError || error.status >= 500) return complete();
    throw error;
  }
}
