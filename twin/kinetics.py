"""Egg-white clouding: liquid temperature in a well, and how fast the white turns opaque.

Model: two parallel first-order reactions, and a saturating optical response (literature review in
twin/README.md, "Kinetics"):
  - ovotransferrin (OT, ~13% of the protein) unfolds and aggregates fast: Ea = 391 kJ/mol, half-life
    1.5 min at 62 C (Shibata-Ishiwatari et al. 2018, whole egg white, DSC kinetics). It makes the white
    milky within minutes from ~60 C, but not white.
  - ovalbumin (OVA, ~54%) does most of the whitening: Ea = 450 kJ/mol (between 408, ibid., and ~480 for
    pure OVA, Weijers et al. 2003). Its rate is fitted to the 2026-10-11 trial.
  - Aggregation follows unfolding quickly, so each reaction is first order with no lag at constant
    temperature (Van der Plancken et al. 2006; Weijers et al. 2003).
  - 3 mm of ~10% protein is optically thick: brightness saturates long before all OVA has aggregated.
    W = (1 - exp(-beta * (w_ot * A_ot + (1 - w_ot) * A_ova))) / (1 - exp(-beta)).
The lag before clouding in a run comes from the liquid warming up behind the bed (first order, fitted),
which also sits a little below the bed, more so in the corner islands (assumed; the protocol's Phase 0
would measure it).

The twin knows these true values; the agent doesn't, so an agent's Arrhenius analysis can be scored
against them (GET /sim/truth on the twin).
"""
import math
from dataclasses import dataclass, field, asdict

R = 8.314462618
T_REF_C = 72.0


def _k_at(ea, k_at_c, t_c, t_ref_c=T_REF_C):
    """Rate at t_ref_c given the rate at another temperature."""
    return k_at_c * math.exp(-ea / R * (1.0 / (t_ref_c + 273.15) - 1.0 / (t_c + 273.15)))


@dataclass
class KineticParams:
    ea_ot: float = 391e3                                   # J/mol (Shibata-Ishiwatari et al. 2018)
    k_ot_ref: float = _k_at(391e3, math.log(2) / 90.0, 62.0)  # 1/s at 72 C, from t1/2 = 1.5 min at 62 C
    ea_ova: float = 450e3                                  # J/mol (408-480 in the literature)
    k_ova_ref: float = 0.00155686                         # 1/s at 72 C; fitted: t1/2 1.9 min at 75 C
    w_ot: float = 0.0225207                               # OT's share: OT alone makes W = 0.22 (milky)
    beta: float = 11.0324                                 # optical saturation (optically thick layer); fitted

    def k_ot(self, t_c):
        return self.k_ot_ref * math.exp(-self.ea_ot / R * (1.0 / (t_c + 273.15) - 1.0 / (T_REF_C + 273.15)))

    def k_ova(self, t_c):
        return self.k_ova_ref * math.exp(-self.ea_ova / R * (1.0 / (t_c + 273.15) - 1.0 / (T_REF_C + 273.15)))


PARAMS = KineticParams()


@dataclass
class ThermalParams:
    tau_s: float = 48.54         # liquid lag behind the plate, s (0.3 mL, ~3 mm deep, oil on top); fitted
    eta_center: float = 0.968    # liquid excess over ambient / plate excess over ambient, center island
    eta_corner: float = 0.952    # corner islands sit ~1 C cooler at 75 C
    t_load_c: float = 21.0       # egg white from the bench


THERMAL = ThermalParams()


@dataclass
class EggWhite:
    """One well's sample: its temperature and the native fraction of each protein population."""
    k_mult: float = 1.0          # egg-to-egg variation of the rates
    eta: float = THERMAL.eta_center
    tau_s: float = THERMAL.tau_s
    temp_c: float = THERMAL.t_load_c
    n_ot: float = 1.0
    n_ova: float = 1.0
    params: KineticParams = field(default_factory=lambda: PARAMS)

    def step(self, plate_c, t_amb, dt):
        target = t_amb + self.eta * (plate_c - t_amb)
        self.temp_c += (target - self.temp_c) * (1.0 - math.exp(-dt / self.tau_s))
        p = self.params
        self.n_ot *= math.exp(-p.k_ot(self.temp_c) * self.k_mult * dt)
        self.n_ova *= math.exp(-p.k_ova(self.temp_c) * self.k_mult * dt)

    def opacity(self):
        """Brightness fraction W in [0, 1]: 0 = raw egg white, 1 = fully cooked."""
        p = self.params
        x = p.w_ot * (1.0 - self.n_ot) + (1.0 - p.w_ot) * (1.0 - self.n_ova)
        return (1.0 - math.exp(-p.beta * x)) / (1.0 - math.exp(-p.beta))


def opacity_after(temp_c, seconds, params=PARAMS):
    """W after holding the liquid at a constant temperature (no warm-up)."""
    w = EggWhite(temp_c=temp_c, eta=1.0, params=params)
    w.n_ot = math.exp(-params.k_ot(temp_c) * seconds)
    w.n_ova = math.exp(-params.k_ova(temp_c) * seconds)
    return w.opacity()


def t_half_isothermal(temp_c, params=PARAMS, frac=0.5, t_max=6 * 3600.0):
    """Time for W to reach `frac` of the protocol's end point (75 C liquid for 10 min after the run),
    holding the liquid at temp_c with no warm-up. Ground truth for scoring an Arrhenius analysis."""
    w_end = opacity_after(75.0, 600.0, params)
    goal = frac * w_end
    if opacity_after(temp_c, t_max, params) < goal:
        return float("nan")
    lo, hi = 0.0, t_max
    for _ in range(60):
        mid = (lo + hi) / 2
        if opacity_after(temp_c, mid, params) >= goal:
            hi = mid
        else:
            lo = mid
    return hi


def truth():
    return {"kinetics": asdict(PARAMS), "thermal": asdict(THERMAL), "t_ref_c": T_REF_C,
            "model": "two parallel first-order reactions (OT, OVA) with saturating optics; liquid lags the plate"}
