/**
 * Editorial copy for the public site. Clinic facts (name, address, hours, phone, services,
 * doctors) come from the API; this file holds the words around them, plus the minimum the
 * site needs to stay useful if the API is briefly unreachable.
 */

import type { ClinicProfile } from "./api/types";

export const FALLBACK_PROFILE: ClinicProfile = {
  name: "Aster Dental Studio",
  tagline: "Unhurried dental care in Indiranagar.",
  timezone: "Asia/Kolkata",
  address: "Indiranagar, Bengaluru",
  map_url: null,
  phone_display: null,
  email: null,
  hours_note: null,
  about: null,
  voice_number: null,
  chat_enabled: false,
};

export const JOURNEY = [
  {
    title: "You choose a time",
    body: "Online, in the chat, or on the phone. You see real openings from the clinic diary, not a request form that someone answers tomorrow.",
  },
  {
    title: "We confirm it",
    body: "The booking is checked against the dentist's calendar. You get a reference straight away, and we tell you plainly if it is still being confirmed.",
  },
  {
    title: "A longer first visit",
    body: "First appointments run long enough to examine, explain and answer questions. Nothing starts until you have agreed the plan and the estimate.",
  },
  {
    title: "Written aftercare",
    body: "You leave with what was done, what to watch for, and when to come back, so you are not trying to remember it all from the chair.",
  },
] as const;

export const FAQ = [
  {
    q: "Do you see patients without an appointment?",
    a: "We keep emergency slots every day for toothache, a broken tooth or a lost filling. Book an emergency pain visit online or call us, and we will fit you in as soon as a chair is free.",
  },
  {
    q: "How much will my treatment cost?",
    a: "It depends on what the dentist finds, so we do not quote prices before an examination. You get a written estimate before any treatment starts, and nothing is added without asking you first.",
  },
  {
    q: "Can I bring my child?",
    a: "Yes. Book a children's check-up for ages two and up. First visits are short and mostly about getting comfortable in the chair.",
  },
  {
    q: "How do I change or cancel a booking?",
    a: "Call us, or ask the receptionist on the phone line with your booking reference ready. You can check a booking's status online with the reference and the phone number you booked with.",
  },
  {
    q: "Who am I talking to in the chat and on the phone?",
    a: "Both are the clinic's virtual assistant. It can find times, book them when you confirm, and pass you to the front desk. It does not give medical advice; for that you will speak to a dentist.",
  },
  {
    q: "Is there parking?",
    a: "There is paid parking on 12th Main, two minutes' walk away. The Indiranagar metro station is about ten minutes on foot.",
  },
] as const;

/** Sample testimonials. They are labelled as sample content wherever they appear. */
export const SAMPLE_REVIEWS = [
  {
    quote: "They showed me the X-ray and explained why the filling could wait six months. I have never had a dentist tell me not to do something.",
    who: "Sample review",
  },
  {
    quote: "Booked a cleaning on my phone at 11 pm and had a reference before I put it down. The appointment itself ran on time.",
    who: "Sample review",
  },
  {
    quote: "My son was nervous. The dentist let him press the chair buttons for five minutes before she looked at a single tooth.",
    who: "Sample review",
  },
] as const;
