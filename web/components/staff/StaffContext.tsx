"use client";

import { createContext, useContext } from "react";

import type { ClinicConfig, StaffUser } from "@/lib/api/types";

export interface StaffContextValue {
  user: StaffUser;
  clinic: ClinicConfig;
  /** Owner and admin can change things; viewers get read-only screens. */
  canWrite: boolean;
  isOwner: boolean;
  timeZone: string;
}

export const StaffContext = createContext<StaffContextValue | null>(null);

export function useStaff(): StaffContextValue {
  const value = useContext(StaffContext);
  if (!value) throw new Error("useStaff must be used inside the staff console");
  return value;
}

export function canWriteRole(role: string): boolean {
  return role === "owner" || role === "admin";
}
