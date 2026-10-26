import { useParams } from "react-router";

export default function JobDetailPage() {
  // React Router decodes path params, so /jobs/a%20b gives "a b".
  const { id } = useParams();
  return (
    <section>
      <h1>Job</h1>
      <p>
        ID: <code>{id}</code>
      </p>
    </section>
  );
}
