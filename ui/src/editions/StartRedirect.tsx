import { Navigate, useLocation } from "react-router-dom";

/** `/start` is Home on a factory cell. The steps for a person joining link to `/start#settings`, so the
 *  query and the hash are kept; the table's `{ redirect }` fallback drops them, so it cannot do this. */
export default function StartRedirect() {
  const { search, hash } = useLocation();
  return <Navigate to={{ pathname: "/", search, hash }} replace />;
}
