import { Navigate, useLocation } from "react-router-dom";

/** `/feed` is Home now (TR-1527): the feed folded into it. This stays for good, because the Slack messages
 *  already sent carry `/feed?item=<id>` links, and they must keep opening at their item. The query is kept;
 *  the table's `{ redirect }` fallback drops it, so it cannot do this. */
export default function FeedRedirect() {
  const { search } = useLocation();
  return <Navigate to={{ pathname: "/", search }} replace />;
}
