import { errorMessage } from "../api/client";

export default function ErrorMessage({ error }: { error: unknown }) {
  if (error === null || error === undefined) {
    return null;
  }
  return <p role="alert">{errorMessage(error)}</p>;
}
