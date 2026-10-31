import "server-only";

import type { ClinicProfile, PublicDoctor, PublicService, Role } from "@/lib/api/types";

/**
 * Development fixtures for mock mode (API_BASE_URL unset, NODE_ENV !== "production").
 * Everything here is synthetic: the clinic, the doctors and every patient are fictional, and
 * phone numbers come from the reserved fictional range +91 90000 0xxxx. This module is only
 * reachable through a dynamic import behind a NODE_ENV check, so production bundles do not
 * contain it.
 */

export const MOCK_TZ = "Asia/Kolkata";
/** Asia/Kolkata has no DST, so mock arithmetic can use a fixed offset. */
export const IST_OFFSET_MINUTES = 330;

export const MOCK_PASSWORD = "aster-demo-2026";

export const MOCK_USERS: readonly { id: string; email: string; role: Role }[] = [
  { id: "0b5d1c1e-7a52-4f0e-9a51-3c1a8f0d0001", email: "owner@asterdental.example", role: "owner" },
  { id: "0b5d1c1e-7a52-4f0e-9a51-3c1a8f0d0002", email: "admin@asterdental.example", role: "admin" },
  { id: "0b5d1c1e-7a52-4f0e-9a51-3c1a8f0d0003", email: "viewer@asterdental.example", role: "viewer" },
];

export const CLINIC_ID = "5a1f3a0e-2f7c-4d0b-8a8e-0c9a0d7e0001";

export const PROFILE: ClinicProfile = {
  name: "Aster Dental Studio",
  tagline: "Unhurried dental care on 100 Feet Road, Indiranagar.",
  timezone: MOCK_TZ,
  address: "2nd floor, 418 100 Feet Road, HAL 2nd Stage, Indiranagar, Bengaluru 560038",
  map_url: "https://www.openstreetmap.org/search?query=100%20Feet%20Road%2C%20Indiranagar%2C%20Bengaluru",
  phone_display: "+91 90000 00100",
  email: "hello@asterdental.example",
  hours_note:
    "Monday to Saturday, 9:30 am to 7:00 pm. Closed on Sundays, and on 8 November for Diwali.",
  about:
    "Aster is a five-chair practice above the bakeries and bookshops of 100 Feet Road. We book " +
    "longer appointments than most clinics so there is time to explain what we see, show you " +
    "the X-ray, and agree a plan before anything starts. Every estimate is written down first.",
  voice_number: "+919000000100",
  chat_enabled: true,
};

interface ServiceSeed extends PublicService {
  buffer: number;
}

export const SERVICES: readonly ServiceSeed[] = [
  {
    id: "7c0e0d1a-0000-4000-8000-000000000001",
    name: "Check-up and cleaning",
    description:
      "An exam, scaling and polish, and X-rays only if they will change what we do. We finish by walking you through what we found.",
    duration_minutes: 45,
    colour: "#3F7D6E",
    buffer: 15,
  },
  {
    id: "7c0e0d1a-0000-4000-8000-000000000002",
    name: "Teeth whitening",
    description:
      "In-chair whitening after a shade check and a look at any sensitive spots. We tell you honestly if a result will not be worth it.",
    duration_minutes: 60,
    colour: "#B58A2E",
    buffer: 15,
  },
  {
    id: "7c0e0d1a-0000-4000-8000-000000000003",
    name: "Tooth-coloured filling",
    description:
      "Composite fillings matched to your tooth, with local anaesthetic. Most single fillings are done in one visit.",
    duration_minutes: 45,
    colour: "#4E6E9E",
    buffer: 15,
  },
  {
    id: "7c0e0d1a-0000-4000-8000-000000000004",
    name: "Root canal consultation",
    description:
      "An endodontist examines the tooth, takes an X-ray and explains whether a root canal is the right call, the number of visits and the cost.",
    duration_minutes: 30,
    colour: "#8C4A62",
    buffer: 0,
  },
  {
    id: "7c0e0d1a-0000-4000-8000-000000000005",
    name: "Tooth extraction",
    description:
      "Simple extractions under local anaesthetic, with written aftercare. Wisdom teeth are assessed first and may need a second visit.",
    duration_minutes: 45,
    colour: "#9A5B3A",
    buffer: 15,
  },
  {
    id: "7c0e0d1a-0000-4000-8000-000000000006",
    name: "Implant consultation",
    description:
      "We check bone and gum health, discuss implant against bridge, and give you a written estimate before you decide.",
    duration_minutes: 30,
    colour: "#5C6B3A",
    buffer: 0,
  },
  {
    id: "7c0e0d1a-0000-4000-8000-000000000007",
    name: "Clear aligner consultation",
    description:
      "An orthodontist checks your bite, takes photos and tells you whether aligners or braces suit you better, and roughly for how long.",
    duration_minutes: 30,
    colour: "#3A7A9A",
    buffer: 0,
  },
  {
    id: "7c0e0d1a-0000-4000-8000-000000000008",
    name: "Children's check-up",
    description:
      "A gentle first look for children from age two, with time to sit in the chair and meet the instruments before anything starts.",
    duration_minutes: 30,
    colour: "#B0603F",
    buffer: 0,
  },
  {
    id: "7c0e0d1a-0000-4000-8000-000000000009",
    name: "Emergency pain visit",
    description:
      "Held back every day for toothache, a broken tooth or a lost filling. We relieve the pain first and plan the rest afterwards.",
    duration_minutes: 30,
    colour: "#A33A3A",
    buffer: 0,
  },
  {
    id: "7c0e0d1a-0000-4000-8000-000000000010",
    name: "Crown fitting",
    description:
      "Fitting a crown made from your earlier impression, with a bite check and adjustments in the same visit.",
    duration_minutes: 60,
    colour: "#6B5B8A",
    buffer: 15,
  },
];

const S = (n: number) => `7c0e0d1a-0000-4000-8000-0000000000${String(n).padStart(2, "0")}`;

interface DoctorSeed extends PublicDoctor {
  /** Weekday (Mon=0) -> [opens, closes] windows in clinic time. */
  hours: Record<number, [string, string][]>;
}

const WEEKDAY_FULL: [string, string][] = [
  ["09:30", "13:30"],
  ["14:30", "19:00"],
];
const MORNINGS: [string, string][] = [["09:30", "13:30"]];
const AFTERNOONS: [string, string][] = [["14:30", "19:00"]];

export const DOCTORS: readonly DoctorSeed[] = [
  {
    id: "d0c70000-0000-4000-8000-000000000001",
    name: "Dr. Rohan Deshpande",
    public_bio:
      "General dentist, BDS (Manipal). Rohan sees most first visits and likes to show patients their own X-rays on the screen before suggesting anything.",
    photo_url: null,
    colour: "#3F7D6E",
    service_ids: [S(1), S(2), S(3), S(9), S(10)],
    hours: { 0: WEEKDAY_FULL, 1: WEEKDAY_FULL, 2: WEEKDAY_FULL, 3: WEEKDAY_FULL, 4: WEEKDAY_FULL, 5: MORNINGS },
  },
  {
    id: "d0c70000-0000-4000-8000-000000000002",
    name: "Dr. Kavya Iyer",
    public_bio:
      "Endodontist, MDS (Conservative Dentistry and Endodontics). Kavya handles root canals and difficult fillings, and explains every step before she starts.",
    photo_url: null,
    colour: "#8C4A62",
    service_ids: [S(1), S(3), S(4), S(10)],
    hours: { 0: WEEKDAY_FULL, 1: AFTERNOONS, 2: WEEKDAY_FULL, 3: AFTERNOONS, 4: WEEKDAY_FULL, 5: WEEKDAY_FULL },
  },
  {
    id: "d0c70000-0000-4000-8000-000000000003",
    name: "Dr. Arjun Menon",
    public_bio:
      "Oral surgeon, MDS (Oral and Maxillofacial Surgery). Arjun does extractions and implant planning, and keeps the clinic's emergency slots.",
    photo_url: null,
    colour: "#9A5B3A",
    service_ids: [S(5), S(6), S(9)],
    hours: { 0: MORNINGS, 1: WEEKDAY_FULL, 2: MORNINGS, 3: WEEKDAY_FULL, 4: MORNINGS, 5: WEEKDAY_FULL },
  },
  {
    id: "d0c70000-0000-4000-8000-000000000004",
    name: "Dr. Farah Siddiqui",
    public_bio:
      "Orthodontist, MDS (Orthodontics). Farah plans aligner and brace treatment for teenagers and adults, and is candid when treatment is optional.",
    photo_url: null,
    colour: "#3A7A9A",
    service_ids: [S(1), S(7)],
    hours: { 1: WEEKDAY_FULL, 3: WEEKDAY_FULL, 5: WEEKDAY_FULL },
  },
  {
    id: "d0c70000-0000-4000-8000-000000000005",
    name: "Dr. Nandini Rao",
    public_bio:
      "Paediatric dentist, MDS (Pedodontics). Nandini sees children from age two and keeps appointments short, unhurried and explained in plain words.",
    photo_url: null,
    colour: "#B0603F",
    service_ids: [S(1), S(3), S(8)],
    hours: { 0: AFTERNOONS, 1: AFTERNOONS, 2: AFTERNOONS, 3: AFTERNOONS, 4: AFTERNOONS, 5: MORNINGS },
  },
];

export const PATIENT_NAMES: readonly string[] = [
  "Ananya Krishnan",
  "Vikram Hegde",
  "Priya Nair",
  "Siddharth Kulkarni",
  "Meghana Reddy",
  "Rahul Bhat",
  "Divya Shetty",
  "Imran Sheikh",
  "Lakshmi Venkatesh",
  "Aditya Joshi",
  "Neha Agarwal",
  "Karthik Subramanian",
  "Pooja Malhotra",
  "Sameer Khan",
  "Shruti Kamath",
  "Varun Gowda",
  "Fatima Ansari",
  "Harish Pillai",
  "Tanvi Desai",
  "Joseph Mathew",
  "Aisha Rahman",
  "Nikhil Rao",
  "Revathi Sundaram",
  "Arnav Chatterjee",
];

export const CALLBACK_REASONS: readonly string[] = [
  "Wants to know if the clinic accepts her insurance for a crown.",
  "Asked about whitening cost before booking.",
  "Daughter's check-up; needs a Saturday morning.",
  "Follow-up on extraction aftercare, some swelling.",
];
