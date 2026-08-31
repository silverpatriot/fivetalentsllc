// Clerk's hosted org-creation flow — not hand-built. Creating the
// organization here fires Clerk's organization.created webhook, which is
// what actually provisions the tenants row (see
// backend/app/api/webhooks_clerk.py). This page is just the entry point.
import { CreateOrganization } from "@clerk/nextjs";

export default function Page() {
  return (
    <div className="flex min-h-screen items-center justify-center">
      <CreateOrganization afterCreateOrganizationUrl="/" />
    </div>
  );
}
