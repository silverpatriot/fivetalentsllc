import type { Metadata } from "next";
import { Geist, Geist_Mono } from "next/font/google";
import { headers } from "next/headers";
import { ClerkProvider } from "@clerk/nextjs";
import { TenantOrgSync } from "@/components/tenant-org-sync";
import "./globals.css";

const geistSans = Geist({
  variable: "--font-geist-sans",
  subsets: ["latin"],
});

const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
});

export const metadata: Metadata = {
  title: "Kerygma",
  description: "AI-assisted sermon generation and transcription for churches.",
};

export default async function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  // Set by middleware.ts from the request's Host header — see
  // lib/tenant.ts. null on the bare marketing domain/localhost.
  const tenantSlug = (await headers()).get("x-tenant-slug");

  return (
    <ClerkProvider>
      <html lang="en">
        <body
          className={`${geistSans.variable} ${geistMono.variable} antialiased`}
        >
          <TenantOrgSync tenantSlug={tenantSlug} />
          {children}
        </body>
      </html>
    </ClerkProvider>
  );
}
