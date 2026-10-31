"use client";

import { useMemo } from "react";
import useSWR from "swr";

import type { AdminDoctor, AdminService, PublicDoctor, PublicService } from "./api/types";

/**
 * Doctor and service names for staff screens. Appointments carry ids only, so names come
 * from the admin lists (all roles may read them) and the public catalogue (which also says
 * which doctor offers which service, for the staff booking dialog).
 */
export function useCatalog() {
  const adminDoctors = useSWR<AdminDoctor[]>("/api/admin/doctors");
  const adminServices = useSWR<AdminService[]>("/api/admin/services");
  const publicDoctors = useSWR<PublicDoctor[]>("/api/public/doctors");
  const publicServices = useSWR<PublicService[]>("/api/public/services");

  return useMemo(() => {
    const doctorNames = new Map<string, string>();
    for (const d of publicDoctors.data ?? []) doctorNames.set(d.id, d.name);
    for (const d of adminDoctors.data ?? []) doctorNames.set(d.id, d.name);
    const serviceNames = new Map<string, string>();
    for (const s of publicServices.data ?? []) serviceNames.set(s.id, s.name);
    for (const s of adminServices.data ?? []) serviceNames.set(s.id, s.name);
    const colours = new Map<string, string | null>();
    for (const s of publicServices.data ?? []) colours.set(s.id, s.colour);
    return {
      doctorNames,
      serviceNames,
      colours,
      adminDoctors: adminDoctors.data,
      adminServices: adminServices.data,
      publicDoctors: publicDoctors.data,
      publicServices: publicServices.data,
      loading: !publicDoctors.data || !publicServices.data,
      error: publicDoctors.error ?? publicServices.error ?? null,
    };
  }, [adminDoctors.data, adminServices.data, publicDoctors.data, publicServices.data, publicDoctors.error, publicServices.error]);
}

/** Services a doctor offers, from the public catalogue. */
export function servicesForDoctor(
  doctorId: string,
  doctors: readonly PublicDoctor[] | undefined,
  services: readonly PublicService[] | undefined,
): PublicService[] {
  const doctor = doctors?.find((d) => d.id === doctorId);
  if (!doctor || !services) return [];
  return services.filter((s) => doctor.service_ids.includes(s.id));
}
