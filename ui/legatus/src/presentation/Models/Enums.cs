namespace IA.Legatus.Models;

// First member is always Unknown (== default) so tolerant parsing falls back to it
// for values absent or added after this schema was written (doc §2: defensive reading).

public enum Phase { Unknown = 0, Think, Plan, Build, Review, Test, Deliver, Address, Reflect }

public enum PhaseStatus { Unknown = 0, Pending, InProgress, Done, Blocked }

public enum BattleStatus { Unknown = 0, Active, Blocked, Closed }

public enum Verdict { Unknown = 0, Accept, AcceptWithOpportunity, Revise, Reject }
