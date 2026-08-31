"use client";

import { useEffect, useState } from "react";
import { useOrganizationList, useOrganization } from "@clerk/nextjs";

/**
 * The other half of subdomain routing (see frontend/lib/tenant.ts and
 * middleware.ts for the hostname -> slug half). Once signed in, if the
 * visitor's active Clerk organization doesn't match the org this
 * subdomain belongs to, switch to it — so visiting
 * gracecommunity.kerygma.church always puts you in Grace Community's
 * workspace, not whichever org you happened to have active last.
 *
 * If the signed-in user isn't a member of this subdomain's org at all,
 * say so rather than silently leaving them in a different tenant's
 * workspace while the URL claims to be this one.
 */
export function TenantOrgSync({ tenantSlug }: { tenantSlug: string | null }) {
  const { organization } = useOrganization();
  const { userMemberships, setActive, isLoaded } = useOrganizationList({
    userMemberships: { infinite: true },
  });
  const [notAMember, setNotAMember] = useState(false);

  useEffect(() => {
    if (!tenantSlug || !isLoaded || !userMemberships?.data) return;
    if (organization?.slug === tenantSlug) {
      setNotAMember(false);
      return;
    }
    const match = userMemberships.data.find((m) => m.organization.slug === tenantSlug);
    if (match && setActive) {
      setActive({ organization: match.organization.id });
      setNotAMember(false);
    } else {
      setNotAMember(true);
    }
  }, [tenantSlug, isLoaded, userMemberships, organization, setActive]);

  if (!tenantSlug || !notAMember) return null;

  return (
    <div className="bg-destructive/10 text-destructive border-destructive/20 border-b px-4 py-2 text-center text-sm">
      You&apos;re signed in, but you&apos;re not a member of this church&apos;s workspace ({tenantSlug}).
    </div>
  );
}
