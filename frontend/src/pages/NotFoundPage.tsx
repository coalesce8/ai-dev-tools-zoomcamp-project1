import { Link } from "react-router";

export default function NotFoundPage() {
  return (
    <section>
      <p>Page not found</p>
      <Link to="/">Back to jobs</Link>
    </section>
  );
}
