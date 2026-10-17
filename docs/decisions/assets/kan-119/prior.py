"""Prior classification of the excluded tickers (general knowledge, 2026-10-17).

A belief per ticker, to be confirmed or overridden by the IB probe. Categories:
renamed (same company, new symbol), same_ticker (still trades as itself),
entity (same ticker, a different company for part of the window), acquired,
failed (bankruptcy / wind-down), gap (priced, with a few missing sessions).
"""

ACQUIRED = """
EA HOLX IPG K HES JNPR WBA DFS ANSS MRO WRK CMA PXD ATVI DISH NLSN CTXS
CERN PBCT XLNX DRE KSU ALXN INFO FLIR VAR ABMD TWTR CXO TIF ETFC NBL CTLT AGN
RTN XEC CELG STI VIAB APC RHT TSS MXIM LLL ESRX NFX SCG AET CA COL SRCL EVHC
GGP XL CSRA MON TWX BCR SNI LVLT ANDV RAI SPLS WFM MJN SWN TGNA HAR LLTC SE
STJ LM DAY BHI PX MYL DNB YHOO FL JWN HBI PDCO SEE TYC EMC HOT CPGX DO CTRA
WCG
""".split()

FAILED = "SIVB FRC BBBY MNK ENDP FTR CHK".split()

RENAMED = {
    "FB": "META", "ANTM": "ELV", "PCLN": "BKNG", "ABC": "COR", "PKI": "RVTY",
    "RE": "EG", "FLT": "CPAY", "WLTW": "WTW", "HCN": "WELL", "HCP": "DOC",
    "PEAK": "DOC", "CBG": "CBRE", "COG": "CTRA", "JEC": "J", "TMK": "GL",
    "HRS": "LHX", "SYMC": "GEN", "NLOK": "GEN", "KORS": "CPRI", "COH": "TPR",
    "DPS": "KDP", "BLL": "BALL", "DISCA": "WBD", "CTL": "LUMN", "HFC": "DINO",
    "CDAY": "DAY", "UTX": "RTX", "ARNC": "HWM", "BHGE": "BKR", "DWDP": "DD",
    "VIAC": "PARA", "LUK": "JEF", "DLPH": "APTV", "GPS": "GAP", "LB": "BBWI",
    "ADS": "BFH", "FBHS": "FBIN", "WYN": "TNL", "TSO": "ANDV", "BK": "BNY",
    "PARA": "PSKY", "CBS": "PARA",
}

# Same ticker, a different company for part of the membership window.
ENTITY = {
    "FOX": "Twenty-First Century Fox until the 2019 Disney deal; Fox Corp after",
    "FOXA": "Twenty-First Century Fox until the 2019 Disney deal; Fox Corp after",
    "DOW": "Dow Chemical until the 2017 DowDuPont merger; Dow Inc from 2019",
    "DD": "E. I. du Pont until 2017; DowDuPont renamed DuPont de Nemours in 2019",
    "IR": "Ingersoll-Rand plc (now TT) until 2020; Gardner Denver renamed IR after",
    "Q": "Quintiles IMS in 2017 (renamed IQV); Qnity from 2025",
    "AA": "Alcoa Inc until the 2016 split (renamed ARNC); Alcoa Corp after",
    "UA C": "Under Armour class C, later UA",
    "DISCK": "Discovery class C; converted into WBD class A in 2022 (a different line)",
    "PSKY": "Paramount Skydance holding company from 2025",
    "WBD": "Warner Bros. Discovery; check IB",
}

SAME_TICKER = "LEG AVB EQR COO ODFL UAL XEL PEP PFG NWL QRVO SATS LUMN".split()
GAP = "AVGO EXPD FI FITB SLG".split()
