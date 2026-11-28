import { Link } from 'react-router-dom'

export function NotFoundPage() {
  return (
    <div>
      <h1>Page not found</h1>
      <p>
        We could not find that page. <Link to="/">Go to the dashboard</Link>.
      </p>
    </div>
  )
}
