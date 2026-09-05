/* ==========================================================================
   VenueType configuration templates (R1.5)

   A VenueType supplies DEFAULTS and TERMINOLOGY only. Selecting one when a
   venue is created seeds the venue's configuration; every value can then be
   overridden at venue level. Adding a new business type here is a data change,
   not a code change (R1.4, R1.6).

   Nothing in this file is aquarium-specific in structure — Aquarium is simply
   one entry among several.
   ========================================================================== */

export const venueTypeTemplates = {
  AQUARIUM: {
    id: 'AQUARIUM',
    label: { en: 'Aquarium', th: 'อควาเรียม' },
    /* Terminology drives customer-facing wording without code branching. */
    terms: {
      area: { en: 'Zone', th: 'โซน' },
      experience: { en: 'Experience', th: 'ประสบการณ์' },
      session: { en: 'Entry time', th: 'เวลาเข้าชม' },
      show: { en: 'Show', th: 'โชว์' }
    },
    defaults: {
      sessionRequirement: 'OPTIONAL', // timed entry offered but not forced
      admissionModel: 'GENERAL_ADMISSION',
      reEntryAllowed: false,
      maxAdvanceDays: 90,
      minLeadMinutes: 180,
      sameDayBooking: true,
      holdMinutes: 10,
      limitedThresholdPct: 15,
      showCategories: ['FEEDING', 'PERFORMANCE', 'KIDS', 'EDUCATIONAL', 'SPECIAL']
    }
  },

  WATER_PARK: {
    id: 'WATER_PARK',
    label: { en: 'Water Park', th: 'สวนน้ำ' },
    terms: {
      area: { en: 'Zone', th: 'โซน' },
      experience: { en: 'Attraction', th: 'เครื่องเล่น' },
      session: { en: 'Entry time', th: 'เวลาเข้า' },
      show: { en: 'Show', th: 'การแสดง' }
    },
    defaults: {
      sessionRequirement: 'NOT_USED',
      admissionModel: 'DAY_PASS',
      reEntryAllowed: true,
      maxAdvanceDays: 120,
      minLeadMinutes: 0,
      sameDayBooking: true,
      holdMinutes: 10,
      limitedThresholdPct: 10,
      showCategories: ['PERFORMANCE', 'KIDS', 'SPECIAL']
    }
  },

  MUSEUM: {
    id: 'MUSEUM',
    label: { en: 'Museum', th: 'พิพิธภัณฑ์' },
    terms: {
      area: { en: 'Gallery', th: 'ห้องจัดแสดง' },
      experience: { en: 'Exhibition', th: 'นิทรรศการ' },
      session: { en: 'Timed entry', th: 'รอบเข้าชม' },
      show: { en: 'Talk', th: 'การบรรยาย' }
    },
    defaults: {
      sessionRequirement: 'REQUIRED',
      admissionModel: 'TIMED_ENTRY',
      reEntryAllowed: false,
      maxAdvanceDays: 60,
      minLeadMinutes: 0,
      sameDayBooking: true,
      holdMinutes: 12,
      limitedThresholdPct: 20,
      showCategories: ['EDUCATIONAL', 'SPECIAL']
    }
  },

  THEATRE: {
    id: 'THEATRE',
    label: { en: 'Theatre', th: 'โรงละคร' },
    terms: {
      area: { en: 'Seating section', th: 'โซนที่นั่ง' },
      experience: { en: 'Production', th: 'การแสดง' },
      session: { en: 'Performance', th: 'รอบการแสดง' },
      show: { en: 'Performance', th: 'รอบการแสดง' }
    },
    defaults: {
      sessionRequirement: 'REQUIRED',
      admissionModel: 'RESERVED_SEAT',
      reEntryAllowed: false,
      maxAdvanceDays: 180,
      minLeadMinutes: 30,
      sameDayBooking: true,
      holdMinutes: 8,
      limitedThresholdPct: 12,
      showCategories: ['PERFORMANCE', 'SPECIAL']
    }
  },

  FITNESS: {
    id: 'FITNESS',
    label: { en: 'Fitness / Gym', th: 'ฟิตเนส' },
    terms: {
      area: { en: 'Studio', th: 'สตูดิโอ' },
      experience: { en: 'Class', th: 'คลาส' },
      session: { en: 'Class time', th: 'รอบคลาส' },
      show: { en: 'Group activity', th: 'กิจกรรมกลุ่ม' }
    },
    defaults: {
      sessionRequirement: 'REQUIRED',
      admissionModel: 'CLASS',
      reEntryAllowed: false,
      maxAdvanceDays: 7, // the fitness example from the brief
      minLeadMinutes: 60,
      sameDayBooking: true,
      holdMinutes: 5,
      limitedThresholdPct: 25,
      showCategories: ['PERFORMANCE', 'EDUCATIONAL']
    }
  },

  THEME_PARK: {
    id: 'THEME_PARK',
    label: { en: 'Theme Park', th: 'สวนสนุก' },
    terms: {
      area: { en: 'Land', th: 'โซน' },
      experience: { en: 'Ride', th: 'เครื่องเล่น' },
      session: { en: 'Entry time', th: 'เวลาเข้า' },
      show: { en: 'Parade / Show', th: 'ขบวนพาเหรด / โชว์' }
    },
    defaults: {
      sessionRequirement: 'OPTIONAL',
      admissionModel: 'DAY_PASS',
      reEntryAllowed: true,
      maxAdvanceDays: 120,
      minLeadMinutes: 0,
      sameDayBooking: true,
      holdMinutes: 10,
      limitedThresholdPct: 10,
      showCategories: ['PERFORMANCE', 'KIDS', 'SPECIAL']
    }
  }
};

/* Admission models are configuration, not code paths (R3.1, R3.2).
   Each model is expressed purely through rule primitives. */
export const admissionModels = {
  GENERAL_ADMISSION: { entries: 1, needsSession: false, needsSeat: false, consumesCapacity: true },
  FIXED_DATE: { entries: 1, needsSession: false, needsSeat: false, consumesCapacity: true },
  OPEN_DATE: { entries: 1, needsSession: false, needsSeat: false, consumesCapacity: false, validDays: 90 },
  TIMED_ENTRY: { entries: 1, needsSession: true, needsSeat: false, consumesCapacity: true },
  SESSION_BOOKING: { entries: 1, needsSession: true, needsSeat: false, consumesCapacity: true },
  RESERVED_SEAT: { entries: 1, needsSession: true, needsSeat: true, consumesCapacity: true },
  DAY_PASS: { entries: 99, needsSession: false, needsSeat: false, consumesCapacity: true, sameDayOnly: true },
  MULTI_DAY_PASS: { entries: 99, needsSession: false, needsSeat: false, consumesCapacity: true, validDays: 3 },
  SINGLE_ENTRY: { entries: 1, needsSession: false, needsSeat: false, consumesCapacity: true },
  MULTIPLE_ENTRY: { entries: 10, needsSession: false, needsSeat: false, consumesCapacity: true },
  RE_ENTRY: { entries: 2, needsSession: false, needsSeat: false, consumesCapacity: true, reEntryWindowMinutes: 480 },
  PACKAGE: { entries: 1, needsSession: false, needsSeat: false, consumesCapacity: true, composite: true },
  MEMBERSHIP: { entries: 999, needsSession: false, needsSeat: false, consumesCapacity: false, validDays: 365 },
  SUBSCRIPTION: { entries: 999, needsSession: false, needsSeat: false, consumesCapacity: false, recurring: true },
  CLASS: { entries: 1, needsSession: true, needsSeat: false, consumesCapacity: true },
  RESOURCE_BOOKING: { entries: 1, needsSession: true, needsSeat: false, consumesCapacity: true, resource: true },
  GROUP_TICKET: { entries: 1, needsSession: false, needsSeat: false, consumesCapacity: true, minQty: 10 },
  COMPLIMENTARY: { entries: 1, needsSession: false, needsSeat: false, consumesCapacity: true, price: 0 }
};
