import type { Cloud } from "../cloud";

/** Who may connect, when this person may not (TR-367): the control plane lets only the
 *  workspace's owner connect and disconnect GitHub, Slack and Linear. */
export default function OwnerOnly({ cloud, what }: { cloud: Cloud; what: string }) {
  const email = cloud.current?.owner_email;
  return (
    <p className="help">
      {email ? <>Only the workspace owner, <strong>{email}</strong>, can connect {what}.</>
        : <>Only the workspace owner can connect {what}.</>}
    </p>
  );
}
