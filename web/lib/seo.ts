import type { ClinicProfile, PublicDoctor, PublicService } from "./api/types";

/** schema.org `Dentist` from the clinic profile. Only fields the API supplied are included. */
export function dentistJsonLd(
  profile: ClinicProfile,
  services: readonly PublicService[] | null,
  doctors: readonly PublicDoctor[] | null,
  url: string,
): Record<string, unknown> {
  const data: Record<string, unknown> = {
    "@context": "https://schema.org",
    "@type": "Dentist",
    name: profile.name,
    url,
  };
  if (profile.tagline || profile.about) data.description = profile.about ?? profile.tagline;
  if (profile.address) {
    data.address = {
      "@type": "PostalAddress",
      streetAddress: profile.address,
      addressLocality: "Bengaluru",
      addressRegion: "Karnataka",
      addressCountry: "IN",
    };
  }
  const phone = profile.voice_number ?? profile.phone_display;
  if (phone) data.telephone = phone;
  if (profile.email) data.email = profile.email;
  if (profile.map_url) data.hasMap = profile.map_url;
  if (profile.hours_note) data.openingHours = profile.hours_note;
  if (services?.length) {
    data.availableService = services.map((service) => ({
      "@type": "MedicalProcedure",
      name: service.name,
      ...(service.description ? { description: service.description } : {}),
    }));
  }
  if (doctors?.length) {
    data.employee = doctors.map((doctor) => ({ "@type": "Person", name: doctor.name, jobTitle: "Dentist" }));
  }
  return data;
}

/** JSON for a <script type="application/ld+json">: `<` is escaped so no value can close the tag. */
export function jsonLdString(data: unknown): string {
  return JSON.stringify(data).replace(/</g, "\\u003c");
}
