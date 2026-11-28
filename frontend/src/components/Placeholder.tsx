export function Placeholder({ title }: { title: string }) {
  return (
    <div>
      <h1>{title}</h1>
      <div className="notice">
        <p style={{ margin: 0 }}>Coming in a later step.</p>
      </div>
    </div>
  )
}
