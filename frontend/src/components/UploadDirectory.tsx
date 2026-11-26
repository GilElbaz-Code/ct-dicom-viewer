import { Alert, Button, Group, Paper, Progress, Stack, Text, Title } from "@mantine/core";
import { useState } from "react";
import { api, type UploadFileSpec } from "../api";

const SESSION_KEY = "ct-viewer-pending-upload";

export function UploadDirectory({ onAccepted }: { onAccepted: () => void }) {
  const [files, setFiles] = useState<File[]>([]);
  const [sessionId, setSessionId] = useState<string | null>(() => localStorage.getItem(SESSION_KEY));
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [progress, setProgress] = useState(0);

  async function upload() {
    setBusy(true);
    setError("");
    try {
      if (files.length < 1 || files.length > 800 || files.some((file) => file.size > 64 * 1024 ** 2)
        || files.reduce((sum, file) => sum + file.size, 0) > 1024 ** 3) {
        throw new Error("Choose up to 800 files, at most 64 MiB per file and 1 GiB total.");
      }
      const ordered = [...files].sort((a, b) => a.webkitRelativePath.localeCompare(b.webkitRelativePath));
      const specs: UploadFileSpec[] = [];
      // Hash one file at a time, keeping memory bounded rather than loading the volume.
      for (const [index, file] of ordered.entries()) {
        setMessage(`Checking file ${index + 1} of ${ordered.length}`);
        const digest = await crypto.subtle.digest("SHA-256", await file.arrayBuffer());
        const sha256 = Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
        specs.push({ name: file.webkitRelativePath.split("/").slice(1).join("/") || file.name, size: file.size, sha256 });
      }
      const session = sessionId ? await api.uploadSession(sessionId)
        : await api.createUpload(ordered[0].webkitRelativePath.split("/")[0] || "CT series", specs);
      if (JSON.stringify(session.files.map(({ name, size, sha256 }) => ({ name, size, sha256 }))) !== JSON.stringify(specs)) {
        throw new Error("This directory differs from the saved upload. Reselect the original directory or start a new upload.");
      }
      setSessionId(session.id);
      localStorage.setItem(SESSION_KEY, session.id);
      for (const [index, file] of session.files.entries()) {
        setMessage(`Uploading file ${index + 1} of ${session.files.length}`);
        if (!file.uploaded && file.form) {
          const body = new FormData();
          for (const [key, value] of Object.entries(file.form.fields)) body.append(key, value);
          body.append("file", ordered[index]);
          const response = await fetch(file.form.url, { method: "POST", body });
          if (!response.ok) throw new Error(`Could not upload ${file.name}. Retry to resume with fresh upload credentials.`);
        }
        setProgress(((index + 1) / session.files.length) * 100);
      }
      setMessage("Verifying the directory and accepting the processing job…");
      const job = await api.completeUpload(session.id);
      localStorage.removeItem(SESSION_KEY);
      setSessionId(null);
      setMessage(`Job ${job.id} accepted. You can close the browser and return later.`);
      onAccepted();
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "Upload failed. Retry to resume.");
    } finally {
      setBusy(false);
    }
  }

  return <Paper withBorder p="md">
    <Stack gap="sm">
      <Title order={2} fz="md">Upload a DICOM directory</Title>
      <Text size="sm" c="dimmed">Choose one series from your computer. Files upload directly to storage; processing continues after the job is accepted.</Text>
      {sessionId && <Alert color="blue">An upload is saved. Reselect the same directory to resume, or start a new upload. Incomplete uploads expire after 24 hours.</Alert>}
      <input aria-label="DICOM directory" type="file" multiple disabled={busy}
        ref={(input) => { input?.setAttribute("webkitdirectory", ""); }}
        onChange={(event) => { setFiles(Array.from(event.currentTarget.files ?? [])); setProgress(0); }} />
      <Group>
        <Button disabled={!files.length} loading={busy} onClick={upload}>{sessionId ? "Resume upload and process" : "Upload and process"}</Button>
        {sessionId && <Button variant="subtle" disabled={busy} onClick={() => {
          localStorage.removeItem(SESSION_KEY); setSessionId(null); setError(""); setMessage(""); setProgress(0);
        }}>Start a new upload</Button>}
      </Group>
      {message && <Text size="sm">{message}</Text>}
      {busy && <Progress value={progress} aria-label="Upload progress" />}
      {error && <Alert color="red">{error}</Alert>}
    </Stack>
  </Paper>;
}
