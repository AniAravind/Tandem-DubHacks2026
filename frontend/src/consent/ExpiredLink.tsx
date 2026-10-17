export default function ExpiredLink({ message }: { message: string }) {
  return (
    <section className="consent-card">
      <h1>This link expired</h1>
      <p>{message || "This link expired. Ask Tandem in your chat for a new one."}</p>
    </section>
  );
}
