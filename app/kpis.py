"""KPI registry: every metric and dimension the analytics engine understands.

One place defines, for each KPI, the SQL aggregate, label, unit, which direction is
"better", the source columns and the phrases users type for it. The planner, the SQL
builder, the summariser, the chart builder and the UI all read from here.
"""
from dataclasses import dataclass, field

TABLE = "kpi_multi_vendor.c_dashboard_kpis"
TABLE_SCHEMA = "kpi_multi_vendor"
TABLE_NAME = "c_dashboard_kpis"


@dataclass(frozen=True)
class Metric:
    id: str
    label: str
    sql: str                      # aggregate expression over the KPI table
    unit: str                     # "%", "count", "Erl", "MB", "cells", "sites"
    better: str | None            # "higher", "lower" or None (neutral volume/traffic)
    columns: tuple                # source columns (used to check the KPI has data)
    aliases: tuple = field(default_factory=tuple)  # normalised phrases users type
    description: str = ""
    source: str = "daily"         # which data source (table) the KPI lives in, see SOURCES
    concept: str = ""             # same KPI in another source (e.g. hourly CSSR) shares a concept


def _m(*args, **kwargs) -> Metric:
    return Metric(*args, **kwargs)


METRICS: dict[str, Metric] = {m.id: m for m in [
    _m("cssr_bh", "CSSR BH (%)", "AVG(cssr_bh)", "%", "higher", ("cssr_bh",),
       ("cssr", "cssr bh", "call setup success rate", "call setup success", "call setup",
        "call success rate", "setup success rate", "cssr bh"),
       "Call Setup Success Rate in the busy hour. 2G/3G only."),
    _m("cssr_daily", "CSSR Daily (%)", "AVG(cssr_daily)", "%", "higher", ("cssr_daily",),
       ("cssr daily", "daily cssr", "call setup success rate daily"),
       "Call Setup Success Rate over the whole day. 2G/3G only."),
    _m("data_sr_bh", "Data SR BH (%)", "AVG(data_sr_bh)", "%", "higher", ("data_sr_bh",),
       ("data sr", "data_sr", "data success rate", "data success", "data sr bh", "ps success rate",
        "data session success", "data setup success", "ps sr"),
       "Data (PS) session success rate in the busy hour. All technologies."),
    _m("data_sr_daily", "Data SR Daily (%)", "AVG(data_sr_daily)", "%", "higher", ("data_sr_daily",),
       ("data sr daily", "daily data sr", "data success rate daily", "daily data success rate"),
       "Data (PS) session success rate over the whole day."),
    _m("tch_availability_daily", "Availability (%)", "AVG(tch_availability_daily)", "%", "higher",
       ("tch_availability_daily",),
       ("availability", "tch availability", "network availability", "cell availability",
        "site availability", "uptime", "avail", "available", "tch_availability_daily",
        "tch availability daily"),
       "Share of time cells were in service (daily). All technologies."),
    _m("drop_rate_bh", "TCH Drop Rate BH (%)",
       "100.0 * SUM(tch_drops_bh) / NULLIF(SUM(tch_assignments_bh), 0)", "%", "lower",
       ("tch_drops_bh", "tch_assignments_bh"),
       ("drop rate", "call drop rate", "tch drop rate", "drop percentage", "drop rate bh",
        "call drop", "cdr", "drop", "drop ratio"),
       "TCH drops / TCH assignments x 100 in the busy hour. 2G/3G only."),
    _m("drop_rate_daily", "TCH Drop Rate Daily (%)",
       "100.0 * SUM(tch_drops_daily) / NULLIF(SUM(tch_assignments_daily), 0)", "%", "lower",
       ("tch_drops_daily", "tch_assignments_daily"),
       ("drop rate daily", "daily drop rate", "daily call drop rate"),
       "TCH drops / TCH assignments x 100 over the whole day. 2G/3G only."),
    _m("tch_drops_bh", "TCH Drops BH", "SUM(tch_drops_bh)", "count", "lower", ("tch_drops_bh",),
       ("tch drops bh", "drops bh", "busy hour drops", "tch_drops_bh"),
       "Number of dropped calls in the busy hour (a count, not a rate)."),
    _m("tch_drops_daily", "TCH Drops Daily", "SUM(tch_drops_daily)", "count", "lower",
       ("tch_drops_daily",),
       ("tch drops", "call drops", "drops", "dropped calls", "drop count", "tch drop",
        "number of drops", "tch_drops_daily", "tch drops daily"),
       "Number of dropped calls over the whole day (a count, not a rate)."),
    _m("tch_assignments_daily", "TCH Assignments Daily", "SUM(tch_assignments_daily)", "count", None,
       ("tch_assignments_daily",),
       ("tch assignments", "assignments", "call attempts", "tch assignment", "tch_assignments_daily"),
       "Number of TCH (voice channel) assignments over the day."),
    _m("cs_traffic_daily", "CS Traffic Daily (Erl)", "SUM(cs_traffic_daily)", "Erl", "higher",
       ("cs_traffic_daily",),
       ("traffic", "voice traffic", "cs traffic", "erlang", "erlangs", "call traffic",
        "cs_traffic_daily", "cs traffic daily", "daily traffic"),
       "Circuit-switched (voice) traffic in Erlangs, whole day."),
    _m("cs_traffic_bh", "CS Traffic BH (Erl)", "SUM(cs_traffic_bh)", "Erl", "higher", ("cs_traffic_bh",),
       ("bh traffic", "busy hour traffic", "traffic bh", "cs traffic bh", "cs_traffic_bh"),
       "Circuit-switched (voice) traffic in Erlangs, busy hour."),
    _m("dl_volume_daily", "DL Volume Daily", "SUM(dl_volume_daily)", "MB", "higher", ("dl_volume_daily",),
       ("dl volume", "download volume", "download", "downlink volume", "dl data", "downlink",
        "dl_volume_daily", "dl volume daily", "download data"),
       "Downlink data volume over the whole day (as stored in the KPI table)."),
    _m("dl_volume_bh", "DL Volume BH", "SUM(dl_volume_bh)", "MB", "higher", ("dl_volume_bh",),
       ("dl volume bh", "busy hour download", "dl_volume_bh"),
       "Downlink data volume in the busy hour."),
    _m("ul_volume_daily", "UL Volume Daily", "SUM(ul_volume_daily)", "MB", "higher", ("ul_volume_daily",),
       ("ul volume", "upload volume", "upload", "uplink volume", "uplink", "ul data",
        "ul_volume_daily", "ul volume daily"),
       "Uplink data volume over the whole day."),
    _m("ul_volume_bh", "UL Volume BH", "SUM(ul_volume_bh)", "MB", "higher", ("ul_volume_bh",),
       ("ul volume bh", "busy hour upload", "ul_volume_bh"),
       "Uplink data volume in the busy hour."),
    _m("total_volume_daily", "Total Data Volume Daily",
       "COALESCE(SUM(dl_volume_daily), 0) + COALESCE(SUM(ul_volume_daily), 0)", "MB", "higher",
       ("dl_volume_daily", "ul_volume_daily"),
       ("data volume", "total volume", "payload", "data traffic", "total data", "data usage",
        "volume", "total data volume", "data payload"),
       "Downlink + uplink data volume over the whole day."),
    _m("utilization", "Utilization (%)", "AVG(ultilization)", "%", "lower", ("ultilization",),
       ("utilization", "utilisation", "ultilization", "cell utilization", "congestion", "cell load"),
       "Cell utilization."),
    _m("normalized_utilization", "Normalized Utilization (%)", "AVG(normalized_utlilization)", "%", "lower",
       ("normalized_utlilization",),
       ("normalized utilization", "normalised utilisation", "normalized_utlilization"),
       "Normalized cell utilization."),
    _m("configured_capacity", "Configured Capacity", "AVG(configured_capacity)", "count", None,
       ("configured_capacity",),
       ("configured capacity", "capacity"),
       "Configured cell capacity."),
    _m("cell_count", "Cells", "COUNT(DISTINCT cell_name)", "cells", None, ("cell_name",),
       ("cell count", "number of cells", "how many cells", "kitne cells", "cells count", "total cells",
        "count of cells", "no of cells"),
       "Number of distinct cells."),
    _m("site_count", "Sites", "COUNT(DISTINCT site_name)", "sites", None, ("site_name",),
       ("site count", "number of sites", "how many sites", "kitne sites", "total sites",
        "count of sites", "no of sites", "kitni sites"),
       "Number of distinct sites."),
    _m("down_cells", "Down Cells", "COUNT(DISTINCT CASE WHEN status = 0 THEN cell_name END)", "cells", "lower",
       ("status",),
       ("down cells", "cells down", "outage", "outages", "down", "off air", "offair", "not working",
        "band", "inactive", "dead cells", "down sites", "sites down", "status down", "unavailable cells"),
       "Number of distinct cells that reported status = 0 (out of service) in the period."),
    _m("down_cell_days", "Down Cell-Days", "SUM(CASE WHEN status = 0 THEN 1 ELSE 0 END)", "count", "lower",
       ("status",),
       ("down days", "outage days", "down cell days"),
       "Number of cell-days with status = 0."),
]}


def _rate(nom: str, denom: str, alias: str = "k") -> str:
    return f"100.0 * SUM({alias}.{nom}) / NULLIF(SUM({alias}.{denom}), 0)"


def _t(id, label, sql, unit, better, columns, aliases=(), description="", source="", concept=""):
    return Metric(id, label, sql, unit, better, tuple(columns), tuple(aliases), description, source, concept)


# ---------------------------------------------------------------- hourly dashboard (same KPIs per hour)
_HOURLY = [
    _t("h_cssr", "CSSR Hourly (%)", "AVG(cssr)", "%", "higher", ["cssr"], (), "Call Setup Success Rate per hour (2G/3G).", "hourly", "cssr_bh"),
    _t("h_data_sr", "Data SR Hourly (%)", "AVG(data_sr)", "%", "higher", ["data_sr"], (), "Data session success rate per hour.", "hourly", "data_sr_bh"),
    _t("h_availability", "Availability Hourly (%)", "AVG(tch_availability)", "%", "higher", ["tch_availability"], (),
       "Cell availability per hour.", "hourly", "tch_availability_daily"),
    _t("h_drop_rate", "TCH Drop Rate Hourly (%)", "100.0 * SUM(tch_drops) / NULLIF(SUM(tch_assignments), 0)", "%", "lower",
       ["tch_drops", "tch_assignments"], (), "TCH drops / assignments x 100 per hour (2G/3G).", "hourly", "drop_rate_bh"),
    _t("h_tch_drops", "TCH Drops Hourly", "SUM(tch_drops)", "count", "lower", ["tch_drops"], (), "Dropped calls per hour.", "hourly", "tch_drops_daily"),
    _t("h_cs_traffic", "CS Traffic Hourly (Erl)", "SUM(cs_traffic)", "Erl", "higher", ["cs_traffic"], (), "Voice traffic per hour.", "hourly", "cs_traffic_daily"),
    _t("h_dl_volume", "DL Volume Hourly", "SUM(dl_volume)", "MB", "higher", ["dl_volume"], (), "Downlink data volume per hour.", "hourly", "dl_volume_daily"),
    _t("h_ul_volume", "UL Volume Hourly", "SUM(ul_volume)", "MB", "higher", ["ul_volume"], (), "Uplink data volume per hour.", "hourly", "ul_volume_daily"),
    _t("h_total_volume", "Total Data Volume Hourly", "COALESCE(SUM(dl_volume), 0) + COALESCE(SUM(ul_volume), 0)", "MB", "higher",
       ["dl_volume", "ul_volume"], (), "DL + UL data volume per hour.", "hourly", "total_volume_daily"),
    _t("h_cell_count", "Cells", "COUNT(DISTINCT cell_name)", "cells", None, ["cell_name"], (), "Distinct cells.", "hourly", "cell_count"),
]

# ---------------------------------------------------------------- Huawei 4G LTE (kpi_huawei.lte_main_kpis, hourly)
_LTE = [
    _t("lte_rrc_sr", "LTE RRC Setup SR (%)", _rate("l_rrc_connreq_succ", "l_rrc_connreq_att"), "%", "higher",
       ["l_rrc_connreq_succ", "l_rrc_connreq_att"], ("rrc success rate", "rrc setup success rate", "rrc sr", "rrc setup", "rrc"),
       "RRC connection setup success rate.", "lte"),
    _t("lte_s1_sr", "LTE S1 Signalling SR (%)", _rate("l_s1sig_connest_succ", "l_s1sig_connest_att"), "%", "higher",
       ["l_s1sig_connest_succ", "l_s1sig_connest_att"], ("s1 success rate", "s1 signalling success rate", "s1 sr", "s1 setup"),
       "S1 signalling connection setup success rate.", "lte"),
    _t("lte_erab_sr", "LTE E-RAB Setup SR (%)", _rate("l_e_rab_succest", "l_e_rab_attest"), "%", "higher",
       ["l_e_rab_succest", "l_e_rab_attest"], ("erab success rate", "e rab success rate", "erab setup success rate",
                                               "e rab setup success rate", "erab sr", "erab setup", "e rab setup"),
       "E-RAB setup success rate.", "lte"),
    _t("lte_cssr", "LTE CSSR (%)",
       "100.0 * (SUM(k.l_rrc_connreq_succ) / NULLIF(SUM(k.l_rrc_connreq_att), 0))"
       " * (SUM(k.l_s1sig_connest_succ) / NULLIF(SUM(k.l_s1sig_connest_att), 0))"
       " * (SUM(k.l_e_rab_succest) / NULLIF(SUM(k.l_e_rab_attest), 0))", "%", "higher",
       ["l_rrc_connreq_succ", "l_s1sig_connest_succ", "l_e_rab_succest"],
       ("lte cssr", "4g cssr", "lte call setup success rate", "lte accessibility", "4g accessibility", "accessibility"),
       "LTE accessibility = RRC SR x S1 SR x E-RAB SR.", "lte"),
    _t("lte_erab_drop", "LTE E-RAB Drop Rate (%)",
       "100.0 * SUM(k.l_e_rab_abnormrel) / NULLIF(SUM(k.l_e_rab_abnormrel) + SUM(k.l_e_rab_normrel), 0)", "%", "lower",
       ["l_e_rab_abnormrel", "l_e_rab_normrel"], ("erab drop rate", "e rab drop rate", "erab drop", "lte drop rate",
                                                  "4g drop rate", "ps drop rate", "retainability", "lte retainability"),
       "Abnormal E-RAB releases / all E-RAB releases.", "lte"),
    _t("lte_volte_sr", "VoLTE Call Success Rate (%)", _rate("volte_call_success", "volte_call_attemps"), "%", "higher",
       ["volte_call_success", "volte_call_attemps"], ("volte success rate", "volte call success rate", "volte sr", "volte cssr", "volte"),
       "VoLTE call setup success rate.", "lte"),
    _t("lte_volte_erab_sr", "VoLTE E-RAB Setup SR (%)", _rate("l_e_rab_succest_qci_1", "l_e_rab_attest_qci_1"), "%", "higher",
       ["l_e_rab_succest_qci_1", "l_e_rab_attest_qci_1"], ("volte erab success rate", "volte erab sr", "qci1 setup success"),
       "QCI-1 (voice) E-RAB setup success rate.", "lte"),
    _t("lte_volte_drop", "VoLTE Drop Rate (%)",
       "100.0 * SUM(k.l_e_rab_abnormrel_qci_1) / NULLIF(SUM(k.l_e_rab_abnormrel_qci_1) + SUM(k.l_e_rab_normrel_qci_1), 0)",
       "%", "lower", ["l_e_rab_abnormrel_qci_1", "l_e_rab_normrel_qci_1"], ("volte drop rate", "volte drop", "volte dcr"),
       "Abnormal QCI-1 releases / all QCI-1 releases.", "lte"),
    _t("lte_volte_traffic", "VoLTE Traffic (Erl)", "SUM(k.volte_session_time_erl)", "Erl", "higher", ["volte_session_time_erl"],
       ("volte traffic", "volte erlang"), "VoLTE session time in Erlangs.", "lte"),
    _t("lte_intra_ho_sr", "LTE Intra-Freq HO SR (%)",
       _rate("lte_intra_frequency_ho_success_rate_nom", "lte_intra_frequency_ho_success_rate_denom"), "%", "higher",
       ["lte_intra_frequency_ho_success_rate_nom"], ("intra frequency handover success rate", "intra freq ho sr", "intra frequency ho"),
       "Intra-frequency handover success rate.", "lte"),
    _t("lte_ho_sr", "LTE Handover SR (%)",
       "100.0 * (SUM(k.l_hho_intraenb_intrafreq_execsuccout) + SUM(k.l_hho_interenb_intrafreq_execsuccout)"
       " + SUM(k.l_hho_intraenb_interfreq_execsuccout) + SUM(k.l_hho_interenb_interfreq_execsuccout))"
       " / NULLIF(SUM(k.l_hho_intraenb_intrafreq_execattout) + SUM(k.l_hho_interenb_intrafreq_execattout)"
       " + SUM(k.l_hho_intraenb_interfreq_execattout) + SUM(k.l_hho_interenb_interfreq_execattout), 0)", "%", "higher",
       ["l_hho_intraenb_intrafreq_execattout"], ("handover success rate", "ho success rate", "hosr", "ho sr", "handover", "lte handover"),
       "Outgoing intra+inter eNB, intra+inter frequency handover success rate.", "lte"),
    _t("lte_csfb_sr", "CSFB Preparation SR (%)", _rate("l_csfb_prepsucc", "l_csfb_prepatt"), "%", "higher",
       ["l_csfb_prepsucc", "l_csfb_prepatt"], ("csfb success rate", "csfb sr", "csfb"), "CS fallback preparation success rate.", "lte"),
    _t("lte_dl_prb_util", "LTE DL PRB Utilization (%)", _rate("l_chmeas_prb_dl_used_avg", "l_chmeas_prb_dl_avail"), "%", "lower",
       ["l_chmeas_prb_dl_used_avg", "l_chmeas_prb_dl_avail"], ("dl prb utilization", "prb utilization", "prb utilisation",
                                                              "prb usage", "dl prb", "prb", "lte utilization", "4g utilization"),
       "Average used DL PRBs / available DL PRBs.", "lte"),
    _t("lte_ul_prb_util", "LTE UL PRB Utilization (%)", _rate("l_chmeas_prb_ul_used_avg", "l_chmeas_prb_ul_avail"), "%", "lower",
       ["l_chmeas_prb_ul_used_avg", "l_chmeas_prb_ul_avail"], ("ul prb utilization", "ul prb", "uplink prb"),
       "Average used UL PRBs / available UL PRBs.", "lte"),
    _t("lte_dl_user_thp", "LTE DL User Throughput (Mbps)", "AVG(k.dl_non_gbr_active_data_user_throughput_mbps)", "Mbps", "higher",
       ["dl_non_gbr_active_data_user_throughput_mbps"], ("user throughput", "dl user throughput", "throughput", "speed",
                                                         "download speed", "lte throughput", "4g throughput"),
       "Average of hourly DL active-user throughput.", "lte"),
    _t("lte_dl_cell_thp", "LTE DL Cell Throughput (Mbps)",
       "SUM(k.l_thrp_bits_dl) / NULLIF(SUM(k.l_thrp_time_cell_dl_highprecision), 0) / 1000.0", "Mbps", "higher",
       ["l_thrp_bits_dl", "l_thrp_time_cell_dl_highprecision"], ("cell throughput", "dl cell throughput"),
       "DL bits / DL cell active time.", "lte"),
    _t("lte_ul_cell_thp", "LTE UL Cell Throughput (Mbps)",
       "SUM(k.l_thrp_bits_ul) / NULLIF(SUM(k.l_thrp_time_cell_ul_highprecision), 0) / 1000.0", "Mbps", "higher",
       ["l_thrp_bits_ul", "l_thrp_time_cell_ul_highprecision"], ("ul cell throughput", "uplink throughput", "ul throughput"),
       "UL bits / UL cell active time.", "lte"),
    _t("lte_dl_volume", "LTE DL Volume (GB)", "SUM(k.l_thrp_bits_dl) / 8e9", "GB", "higher", ["l_thrp_bits_dl"],
       ("lte dl volume", "lte download volume", "4g dl volume", "lte payload", "4g payload"), "Downlink PDCP volume.", "lte"),
    _t("lte_ul_volume", "LTE UL Volume (GB)", "SUM(k.l_thrp_bits_ul) / 8e9", "GB", "higher", ["l_thrp_bits_ul"],
       ("lte ul volume", "lte upload volume", "4g ul volume"), "Uplink PDCP volume.", "lte"),
    _t("lte_packet_loss", "LTE DL Packet Loss (%)", _rate("packet_loss_rate_nom", "packet_loss_rate_denom"), "%", "lower",
       ["packet_loss_rate_nom", "packet_loss_rate_denom"], ("packet loss", "packet loss rate"), "DL packet loss rate.", "lte"),
    _t("lte_cqi", "LTE Average CQI", "SUM(k.average_cqi_nom) / NULLIF(SUM(k.average_cqi_denom), 0)", "index", "higher",
       ["average_cqi_nom", "average_cqi_denom"], ("cqi", "average cqi", "channel quality"), "Average channel quality indicator (0-15).", "lte"),
    _t("lte_active_users", "LTE Active Users (avg)", "AVG(k.dl_non_gbr_active_data_users)", "users", None,
       ["dl_non_gbr_active_data_users"], ("active users", "lte users", "4g users", "users"), "Average DL active data users per cell-hour.", "lte"),
    _t("lte_max_users", "LTE Max Active Users", "MAX(k.l_traffic_activeuser_max)", "users", None, ["l_traffic_activeuser_max"],
       ("max users", "peak users", "maximum users"), "Peak active users.", "lte"),
    _t("lte_availability", "LTE Cell Availability (%)", "100.0 * SUM(k.l_cell_avail_dur) / NULLIF(3600.0 * COUNT(k.l_cell_avail_dur), 0)",
       "%", "higher", ["l_cell_avail_dur"], ("lte availability", "4g cell availability", "lte cell availability"),
       "Cell available time / total time (includes energy-saving and manual outages).", "lte"),
    _t("lte_sys_unavail", "LTE System Unavailability (%)", "100.0 * SUM(k.l_cell_unavail_dur_sys) / NULLIF(3600.0 * COUNT(k.l_cell_unavail_dur_sys), 0)",
       "%", "lower", ["l_cell_unavail_dur_sys"], ("system unavailability", "lte outage", "lte unavailability"),
       "Time unavailable because of system faults / total time.", "lte"),
]

# ---------------------------------------------------------------- Huawei 5G NR (kpi_huawei.nr_main_kpis, hourly, NSA)
_NR = [
    _t("nr_sgnb_add_sr", "5G SgNB Addition SR (%)", _rate("n_nsadc_sgnb_add_succ", "n_nsadc_sgnb_add_att"), "%", "higher",
       ["n_nsadc_sgnb_add_succ", "n_nsadc_sgnb_add_att"], ("sgnb addition success rate", "sgnb add success rate", "sgnb addition",
                                                           "sgnb", "5g accessibility", "nr accessibility", "endc setup"),
       "EN-DC SgNB addition success rate (5G accessibility).", "nr"),
    _t("nr_sgnb_abnormal_rel", "5G SgNB Abnormal Release (%)", _rate("n_nsadc_sgnb_abnormrel", "n_nsadc_sgnb_rel"), "%", "lower",
       ["n_nsadc_sgnb_abnormrel", "n_nsadc_sgnb_rel"], ("sgnb abnormal release", "5g drop rate", "nr drop rate", "5g retainability",
                                                       "sgnb drop"), "Abnormal SgNB releases / SgNB releases.", "nr"),
    _t("nr_pscell_change_sr", "5G PSCell Change SR (%)",
       "100.0 * (SUM(k.n_nsadc_intersgnb_pscell_change_succ) + SUM(k.n_nsadc_intrasgnb_pscell_change_succ))"
       " / NULLIF(SUM(k.n_nsadc_intersgnb_pscell_change_att) + SUM(k.n_nsadc_intrasgnb_pscell_change_att), 0)", "%", "higher",
       ["n_nsadc_intersgnb_pscell_change_att"], ("pscell change success rate", "pscell change", "5g mobility", "5g handover"),
       "Intra + inter SgNB PSCell change success rate.", "nr"),
    _t("nr_availability", "5G Cell Availability (%)",
       "100.0 - 100.0 * (SUM(k.n_cell_unavail_dur_system) + SUM(k.n_cell_unavail_dur_manual)) / NULLIF(3600.0 * COUNT(*), 0)",
       "%", "higher", ["n_cell_unavail_dur_system"], ("5g availability", "nr availability", "5g cell availability"),
       "100 - (system + manual unavailable time) / total time.", "nr"),
    _t("nr_avg_users", "5G RRC Users (avg)", "AVG(k.n_user_rrcconn_avg)", "users", None, ["n_user_rrcconn_avg"],
       ("5g users", "nr users", "5g rrc users"), "Average RRC-connected users per cell-hour.", "nr"),
    _t("nr_max_users", "5G RRC Users (max)", "MAX(k.n_user_rrcconn_max)", "users", None, ["n_user_rrcconn_max"],
       ("5g max users", "5g peak users"), "Peak RRC-connected users.", "nr"),
]

# ---------------------------------------------------------------- Huawei 3G UMTS (kpi_huawei.umts_main_kpis, hourly)
_UMTS = [
    _t("umts_voice_cssr", "3G Voice CSSR (%)",
       "100.0 * (SUM(k.voice_cssr_nom1) / NULLIF(SUM(k.voice_cssr_denom1), 0)) * (SUM(k.voice_cssr_nom2) / NULLIF(SUM(k.voice_cssr_denom2), 0))",
       "%", "higher", ["voice_cssr_nom1", "voice_cssr_denom1"], ("3g voice cssr", "umts cssr", "3g cssr", "voice cssr", "rrc rab cssr"),
       "RRC SR x CS RAB SR.", "umts"),
    _t("umts_voice_cdr", "3G Voice Call Drop Rate (%)", _rate("voice_cdr_nom", "voice_cdr_denom"), "%", "lower",
       ["voice_cdr_nom", "voice_cdr_denom"], ("3g call drop rate", "3g voice drop", "umts drop rate", "3g cdr", "voice call drop rate",
                                             "voice cdr"), "CS voice call drop rate.", "umts"),
    _t("umts_data_setup_sr", "3G Data Setup SR (%)",
       "100.0 * (SUM(k.data_setup_sr_nom1) / NULLIF(SUM(k.data_setup_sr_denom1), 0)) * (SUM(k.data_setup_sr_nom2) / NULLIF(SUM(k.data_setup_sr_denom2), 0))",
       "%", "higher", ["data_setup_sr_nom1", "data_setup_sr_denom1"], ("3g data setup success rate", "3g data sr", "umts data sr",
                                                                       "ps setup success rate", "3g ps cssr"), "PS RRC SR x PS RAB SR.", "umts"),
    _t("umts_sho_sr", "3G Soft HO SR (%)", _rate("sho_nom", "sho_denom"), "%", "higher", ["sho_nom", "sho_denom"],
       ("soft handover success rate", "sho success rate", "soft ho", "sho sr", "3g handover"), "Soft handover success rate.", "umts"),
    _t("umts_irat_ho_sr", "3G IRAT HO SR (%)", _rate("iratho_succoutcs", "iratho_attoutcs"), "%", "higher",
       ["iratho_succoutcs", "iratho_attoutcs"], ("irat handover success rate", "irat ho", "3g to 2g handover", "irat"),
       "CS inter-RAT (3G to 2G) handover success rate.", "umts"),
    _t("umts_availability", "3G Cell Availability (%)", "100.0 - 100.0 * SUM(k.vs_cell_unavailtime) / NULLIF(3600.0 * COUNT(k.vs_cell_unavailtime), 0)",
       "%", "higher", ["vs_cell_unavailtime"], ("3g availability", "umts availability", "3g cell availability"),
       "100 - unavailable time / total time.", "umts"),
    _t("umts_cs_traffic", "3G CS Traffic (Erl)", "SUM(k.cs_traffic)", "Erl", "higher", ["cs_traffic"],
       ("3g traffic", "3g voice traffic", "umts traffic", "3g erlang"), "CS voice traffic.", "umts"),
    _t("umts_hsdpa_volume", "3G HSDPA Volume", "SUM(k.hsdpa_volume)", "MB", "higher", ["hsdpa_volume"],
       ("hsdpa volume", "hsdpa traffic", "hsdpa"), "HSDPA (downlink) data volume.", "umts"),
    _t("umts_hsupa_volume", "3G HSUPA Volume", "SUM(k.hsupa_volume)", "MB", "higher", ["hsupa_volume"],
       ("hsupa volume", "hsupa traffic", "hsupa"), "HSUPA (uplink) data volume.", "umts"),
    _t("umts_data_volume", "3G Total Data Volume",
       "COALESCE(SUM(k.hsdpa_volume), 0) + COALESCE(SUM(k.hsupa_volume), 0) + COALESCE(SUM(k.mobile_data_dl_rel99), 0) + COALESCE(SUM(k.mobile_data_ulrel99), 0)",
       "MB", "higher", ["hsdpa_volume"], ("3g data volume", "umts data volume", "3g payload"), "HSDPA + HSUPA + R99 volume.", "umts"),
    _t("umts_hsdpa_thp", "3G HSDPA Throughput (kbps)", "AVG(k.vs_hsdpa_meanchthroughput)", "kbps", "higher", ["vs_hsdpa_meanchthroughput"],
       ("hsdpa throughput", "3g throughput", "umts throughput", "3g speed"), "Average HSDPA mean channel throughput.", "umts"),
    _t("umts_rtwp", "3G RTWP (dBm)", "AVG(k.meanrtw)", "dBm", "lower", ["meanrtw"],
       ("rtwp", "uplink interference", "ul interference", "3g interference"), "Mean received total wideband power (higher = more interference).", "umts"),
    _t("umts_ecno", "3G Ec/No (dB)", "AVG(k.ecno)", "dB", "higher", ["ecno"], ("ecno", "ec no", "3g quality"),
       "Average Ec/No (higher is better).", "umts"),
    _t("umts_tcp_util", "3G TCP Utilization (%)", "AVG(k.tcp_utilization)", "%", "lower", ["tcp_utilization"],
       ("tcp utilization", "power utilization", "3g utilization", "3g load"), "Transmitted carrier power utilization.", "umts"),
    _t("umts_voice_users", "3G Voice Users (avg)", "AVG(k.voice_users)", "users", None, ["voice_users"], ("3g voice users", "3g users"),
       "Average voice users per cell-hour.", "umts"),
]

# ---------------------------------------------------------------- Huawei 2G GSM (kpi_huawei.gsm_main_kpis, hourly)
_GSM = [
    _t("gsm_sdcch_cong", "2G SDCCH Congestion (%)",
       "100.0 * SUM(k.k3001_failed_sdcch_seizures_due_to_busy_sdcch) / NULLIF(SUM(k.k3000_sdcch_seizure_requests), 0)", "%", "lower",
       ["k3001_failed_sdcch_seizures_due_to_busy_sdcch", "k3000_sdcch_seizure_requests"],
       ("sdcch congestion", "sdcch blocking", "sdcch cong", "signalling congestion"), "Failed SDCCH seizures (busy) / requests.", "gsm"),
    _t("gsm_sdcch_drop", "2G SDCCH Drop Rate (%)",
       "100.0 * SUM(k.cm30_call_drops_on_sdcch) / NULLIF(SUM(k.k3003_successful_sdcch_seizures), 0)", "%", "lower",
       ["cm30_call_drops_on_sdcch", "k3003_successful_sdcch_seizures"], ("sdcch drop rate", "sdcch drop", "sd drop"),
       "Drops on SDCCH / successful SDCCH seizures.", "gsm"),
    _t("gsm_tch_assign_sr", "2G TCH Assignment SR (%)", _rate("ca313_successful_assignments", "ca310_assignment_requests"), "%", "higher",
       ["ca313_successful_assignments", "ca310_assignment_requests"], ("tch assignment success rate", "assignment success rate",
                                                                      "tch assignment sr", "assignment sr"), "Successful / requested TCH assignments.", "gsm"),
    _t("gsm_tch_drop_rate", "2G TCH Drop Rate (%)", _rate("tch_drop_nom", "tch_drop_denom"), "%", "lower",
       ["tch_drop_nom", "tch_drop_denom"], ("2g drop rate", "gsm drop rate", "2g tch drop rate", "gsm call drop rate"),
       "TCH drops / TCH seizures.", "gsm"),
    _t("gsm_tch_blocking", "2G TCH Blocking (%)", _rate("tch_blocking_nom", "tch_blocking_denom"), "%", "lower",
       ["tch_blocking_nom", "tch_blocking_denom"], ("tch blocking", "tch congestion", "tch cong", "2g blocking", "gsm blocking",
                                                   "voice congestion", "blocking"), "TCH blocking rate.", "gsm"),
    _t("gsm_data_setup_sr", "2G Data Setup SR (%)", _rate("data_set_up_sr_nom", "data_set_up_sr_denom"), "%", "higher",
       ["data_set_up_sr_nom", "data_set_up_sr_denom"], ("2g data setup success rate", "gprs success rate", "tbf success rate", "2g data sr"),
       "GPRS/EDGE data setup success rate.", "gsm"),
    _t("gsm_cs_traffic", "2G CS Traffic (Erl)", "SUM(k.cs_traffic)", "Erl", "higher", ["cs_traffic"],
       ("2g traffic", "gsm traffic", "2g voice traffic", "2g erlang"), "TCH voice traffic.", "gsm"),
    _t("gsm_sd_traffic", "2G SDCCH Traffic (Erl)", "SUM(k.sd_traffic)", "Erl", None, ["sd_traffic"], ("sdcch traffic", "sd traffic"),
       "SDCCH traffic.", "gsm"),
    _t("gsm_hr_share", "2G Half-Rate Traffic Share (%)", "100.0 * SUM(k.half_rate_traffic) / NULLIF(SUM(k.cs_traffic), 0)", "%", None,
       ["half_rate_traffic", "cs_traffic"], ("half rate", "half rate share", "hr share", "half rate traffic"), "Half-rate share of voice traffic.", "gsm"),
    _t("gsm_dl_rxqual", "2G DL RxQual Good Samples (%)", _rate("dl_rxqual_nonbcch_nom", "dl_rxqual_nonbcch_denom"), "%", "higher",
       ["dl_rxqual_nonbcch_nom", "dl_rxqual_nonbcch_denom"], ("dl rxqual", "rxqual", "dl quality", "downlink quality", "2g quality"),
       "Share of good-quality DL RxQual samples (non-BCCH).", "gsm"),
    _t("gsm_ul_rxqual", "2G UL RxQual Good Samples (%)", _rate("ul_rxqual_nonbcch_nom", "ul_rxqual_nonbcch_denom"), "%", "higher",
       ["ul_rxqual_nonbcch_nom", "ul_rxqual_nonbcch_denom"], ("ul rxqual", "ul quality", "uplink quality"),
       "Share of good-quality UL RxQual samples (non-BCCH).", "gsm"),
    _t("gsm_dual_band_ho_sr", "2G Dual-Band HO SR (%)", _rate("k3173_successful_dual_band_handovers", "k3170_dual_band_handover_requests"),
       "%", "higher", ["k3173_successful_dual_band_handovers", "k3170_dual_band_handover_requests"],
       ("dual band handover", "2g handover success rate", "gsm handover", "2g ho sr"), "Dual-band handover success rate.", "gsm"),
    _t("gsm_tch_availability", "2G TCH Availability (%)",
       "100.0 * SUM(k.available_tch_channels) / NULLIF(SUM(k.dynamically_configured_available_tch_channels), 0)", "%", "higher",
       ["available_tch_channels", "dynamically_configured_available_tch_channels"], ("tch availability", "2g tch availability"),
       "Available TCH channels / configured TCH channels.", "gsm"),
    _t("gsm_data_volume", "2G Data Volume", "COALESCE(SUM(k.dl_data_volume_radio), 0) + COALESCE(SUM(k.ul_data_volume_radio), 0)",
       "MB", "higher", ["dl_data_volume_radio"], ("2g data volume", "gprs volume", "edge volume", "2g payload"), "GPRS/EDGE DL + UL volume.", "gsm"),
]

for _metric in _HOURLY + _LTE + _NR + _UMTS + _GSM:
    METRICS[_metric.id] = _metric

# KPI sets used when the user does not name a KPI.
KEY_METRICS = ["cssr_bh", "data_sr_bh", "tch_availability_daily", "drop_rate_bh", "cs_traffic_daily", "dl_volume_daily"]
DEGRADATION_METRICS = ["cssr_bh", "data_sr_bh", "tch_availability_daily", "drop_rate_bh"]
TRAFFIC_METRICS = ["cs_traffic_daily", "dl_volume_daily", "ul_volume_daily"]

# Thresholds used for "breach" counts in analytics and in the summary.
THRESHOLDS = {
    "cssr_bh": ("<", 95.0), "cssr_daily": ("<", 95.0),
    "data_sr_bh": ("<", 95.0), "data_sr_daily": ("<", 95.0),
    "tch_availability_daily": ("<", 99.0),
    "drop_rate_bh": (">", 2.0), "drop_rate_daily": (">", 2.0),
    "h_cssr": ("<", 95.0), "h_data_sr": ("<", 95.0), "h_availability": ("<", 99.0), "h_drop_rate": (">", 2.0),
    "lte_rrc_sr": ("<", 98.0), "lte_erab_sr": ("<", 98.0), "lte_cssr": ("<", 98.0), "lte_erab_drop": (">", 1.0),
    "lte_volte_sr": ("<", 98.0), "lte_volte_drop": (">", 1.0), "lte_ho_sr": ("<", 95.0), "lte_dl_prb_util": (">", 80.0),
    "lte_availability": ("<", 99.0), "nr_sgnb_add_sr": ("<", 98.0), "nr_sgnb_abnormal_rel": (">", 2.0),
    "nr_availability": ("<", 99.0), "umts_voice_cssr": ("<", 98.0), "umts_voice_cdr": (">", 1.0),
    "umts_data_setup_sr": ("<", 98.0), "umts_availability": ("<", 99.0), "gsm_sdcch_cong": (">", 1.0),
    "gsm_tch_blocking": (">", 2.0), "gsm_tch_drop_rate": (">", 2.0), "gsm_tch_assign_sr": ("<", 98.0),
}

# KPIs shown when a technology source is chosen but no KPI is named.
SOURCE_KEY_METRICS = {
    "daily": KEY_METRICS,
    "hourly": ["h_cssr", "h_data_sr", "h_availability", "h_drop_rate", "h_cs_traffic", "h_dl_volume"],
    "lte": ["lte_cssr", "lte_erab_drop", "lte_volte_sr", "lte_ho_sr", "lte_dl_prb_util", "lte_dl_user_thp", "lte_availability"],
    "nr": ["nr_sgnb_add_sr", "nr_sgnb_abnormal_rel", "nr_pscell_change_sr", "nr_availability", "nr_avg_users"],
    "umts": ["umts_voice_cssr", "umts_voice_cdr", "umts_data_setup_sr", "umts_sho_sr", "umts_availability", "umts_hsdpa_thp"],
    "gsm": ["gsm_sdcch_cong", "gsm_tch_blocking", "gsm_tch_drop_rate", "gsm_tch_assign_sr", "gsm_cs_traffic", "gsm_dl_rxqual"],
}


@dataclass(frozen=True)
class Dimension:
    id: str
    sql: str        # expression in SELECT / GROUP BY
    alias: str      # output column name
    label: str
    time: bool = False


DIMENSIONS: dict[str, Dimension] = {d.id: d for d in [
    Dimension("date", "date", "date", "Date", True),
    Dimension("week", "DATE_TRUNC('week', date)::date", "week", "Week (starting)", True),
    Dimension("month", "TO_CHAR(date, 'YYYY-MM')", "month", "Month", True),
    Dimension("region", "COALESCE(region, 'Unknown')", "region", "Region"),
    Dimension("technology", "COALESCE(technology, 'Unknown')", "technology", "Technology"),
    Dimension("vendor", "COALESCE(vendor, 'Unknown')", "vendor", "Vendor"),
    # cells without a site_name are named after their cell ("3G_Andiamo_U900_1" -> "3G_Andiamo")
    Dimension("site", "COALESCE(site_name, REGEXP_REPLACE(cell_name, '_[GULN][0-9]{3,4}[A-Za-z0-9]*(_[0-9]+)*$', ''))",
              "site_name", "Site"),
    Dimension("cell", "cell_name", "cell_name", "Cell"),
    Dimension("status", "CASE WHEN status = 1 THEN 'Up' ELSE 'Down' END", "status", "Status"),
    Dimension("hour", "", "hour", "Hour", True),                 # hourly sources only
    Dimension("hour_of_day", "", "hour_of_day", "Hour of day", True),
]}

# Columns whose values are labels, never chart values.
LABEL_COLUMNS = {"date", "week", "month", "region", "technology", "vendor", "site_name", "cell_name",
                 "status", "did", "latitude", "longitude", "Operator", "hour", "hour_of_day"}

_SITE_FROM_CELL = "REGEXP_REPLACE({cell}, '_[GULN][0-9]{{3,4}}[A-Za-z0-9]*(_[0-9]+)*$', '')"


@dataclass(frozen=True)
class Source:
    """A table (or table + cell join) the analytics engine can query."""
    id: str
    label: str
    from_sql: str                 # FROM clause body
    tables: tuple                 # every table the FROM uses (SQL guard + catalog)
    time_col: str                 # date or timestamp expression
    granularity: str              # "day" | "hour"
    cols: dict                    # site_name / cell_name / region / technology / vendor / status -> SQL or None
    technology: str | None = None # fixed technology for per-technology tables
    description: str = ""

    @property
    def date_expr(self) -> str:
        return self.time_col if self.granularity == "day" else f"{self.time_col}::date"

    def supports(self, dim_id: str) -> bool:
        if dim_id in ("hour", "hour_of_day"):
            return self.granularity == "hour"
        if dim_id == "status":
            return bool(self.cols.get("status"))
        return True

    def dim_sql(self, dim_id: str) -> str:
        t, c = self.time_col, self.cols
        if dim_id == "date":
            return self.date_expr
        if dim_id == "week":
            return f"DATE_TRUNC('week', {t})::date"
        if dim_id == "month":
            return f"TO_CHAR({t}, 'YYYY-MM')"
        if dim_id == "hour":
            return f"TO_CHAR({t}, 'YYYY-MM-DD HH24:00')"
        if dim_id == "hour_of_day":
            return f"EXTRACT(HOUR FROM {t})::int"
        if dim_id == "region":
            return f"COALESCE({c['region']}, 'Unknown')"
        if dim_id == "technology":
            return f"'{self.technology}'" if self.technology else f"COALESCE({c['technology']}, 'Unknown')"
        if dim_id == "vendor":
            return f"COALESCE({c['vendor']}, 'Unknown')" if c.get("vendor") else "'Huawei'"
        if dim_id == "site":
            return f"COALESCE({c['site_name']}, {_SITE_FROM_CELL.format(cell=c['cell_name'])})"
        if dim_id == "cell":
            return c["cell_name"]
        if dim_id == "status":
            return f"CASE WHEN {c['status']} = 1 THEN 'Up' ELSE 'Down' END"
        raise KeyError(dim_id)

    def tech_expr(self) -> str:
        return f"'{self.technology}'" if self.technology else self.cols["technology"]


_PLAIN = {"site_name": "site_name", "cell_name": "cell_name", "region": "region", "technology": "technology",
          "vendor": "vendor", "status": "status"}
_GID = {"site_name": "g.site_name", "cell_name": "g.cell_name", "region": "g.region", "technology": "g.technology",
        "vendor": "g.vendor", "status": None}


def _tech_source(id, label, table, tech, description):
    return Source(id, label, f"{table} k LEFT JOIN gid.cells g ON g.did = k.did::int", (table, "gid.cells"),
                  "k.starttime", "hour", _GID, tech, description)


SOURCES: dict[str, Source] = {s.id: s for s in [
    Source("daily", "Daily KPI dashboard (all technologies)", TABLE, (TABLE,), "date", "day", _PLAIN, None,
           "One row per cell per day: CSSR, data SR, availability, TCH drops/drop rate, traffic, DL/UL volume, cell status. "
           "The default source."),
    Source("hourly", "Hourly KPI dashboard (all technologies)", "kpi_multi_vendor.c_dashboard_kpis_hourly",
           ("kpi_multi_vendor.c_dashboard_kpis_hourly",), "starttime", "hour", {**_PLAIN, "vendor": None, "status": None}, None,
           "Same KPIs as the daily dashboard, one row per cell per hour. Use for hourly trends, busy hour, hour-of-day patterns."),
    _tech_source("gsm", "Huawei 2G GSM KPIs (hourly)", "kpi_huawei.gsm_main_kpis", "2G",
                 "2G counters per cell-hour: SDCCH congestion/drop, TCH blocking/drop/assignment, RxQual, half-rate, data setup."),
    _tech_source("umts", "Huawei 3G UMTS KPIs (hourly)", "kpi_huawei.umts_main_kpis", "3G",
                 "3G counters per cell-hour: voice CSSR/CDR, PS setup, soft/IRAT handover, HSDPA/HSUPA volume and throughput, RTWP, Ec/No."),
    _tech_source("lte", "Huawei 4G LTE KPIs (hourly)", "kpi_huawei.lte_main_kpis", "4G",
                 "4G counters per cell-hour: RRC/S1/E-RAB setup, LTE CSSR, E-RAB drop, VoLTE, handover, CSFB, PRB utilization, "
                 "throughput, volume, CQI, packet loss, users, availability."),
    _tech_source("nr", "Huawei 5G NR KPIs (hourly, NSA)", "kpi_huawei.nr_main_kpis", "5G",
                 "5G NSA counters per cell-hour: SgNB addition, abnormal release, PSCell change, availability, users."),
]}


def metric_source(metric_id: str) -> str:
    return METRICS[metric_id].source or "daily"


def translate_metric(metric_id: str, source_id: str) -> str | None:
    """The same KPI in another source (daily CSSR <-> hourly CSSR), or None."""
    m = METRICS.get(metric_id)
    if not m:
        return None
    if metric_source(metric_id) == source_id:
        return metric_id
    concept = m.concept or m.id
    for other in METRICS.values():
        if metric_source(other.id) == source_id and (other.concept == concept or other.id == concept):
            return other.id
    return None

# Column dictionary exposed at /api/kpi-metadata (kept for backward compatibility).
KPI_DICTIONARY = {
    "cssr_bh": "Call Setup Success Rate (Busy Hour)",
    "cssr_daily": "Call Setup Success Rate (Daily)",
    "tch_availability_daily": "Network Availability",
    "tch_drops_bh": "TCH Drops (Busy Hour, count)",
    "tch_drops_daily": "TCH Drops (Daily, count)",
    "data_sr_bh": "Data Success Rate (Busy Hour)",
    "data_sr_daily": "Data Success Rate (Daily)",
    "ultilization": "Cell Utilization",
    "dl_volume_bh": "Download Volume (Busy Hour)",
    "dl_volume_daily": "Download Volume (Daily)",
    "cs_traffic_bh": "Circuit Switched Traffic (Busy Hour)",
    "cs_traffic_daily": "Circuit Switched Traffic (Daily)",
    "ul_volume_bh": "Upload Volume (Busy Hour)",
    "ul_volume_daily": "Upload Volume (Daily)",
}


def column_label(column: str) -> str:
    """Human label for an output column (metric alias, compare suffixes, dimension)."""
    for suffix, tag in (("_current", " (current)"), ("_previous", " (previous)"), ("_change", " (change)")):
        if column.endswith(suffix) and column[: -len(suffix)] in METRICS:
            return METRICS[column[: -len(suffix)]].label + tag
    if column in METRICS:
        return METRICS[column].label
    for dim in DIMENSIONS.values():
        if dim.alias == column:
            return dim.label
    return KPI_DICTIONARY.get(column, column.replace("_", " ").title())


def metric_of_column(column: str) -> Metric | None:
    for suffix in ("_current", "_previous", "_change"):
        if column.endswith(suffix) and column[: -len(suffix)] in METRICS:
            return METRICS[column[: -len(suffix)]]
    return METRICS.get(column)


# Telecom knowledge base: answers definition questions without the DB or Claude.
TELECOM_KB = {
    "cssr": "**CSSR (Call Setup Success Rate)** — Percentage of call setup attempts that successfully complete. Formula: Successful Seizures / Total Attempts × 100. **Higher is better.** Typical target: ≥ 95%. In this data it is reported for 2G and 3G cells.",
    "cssr_bh": "**CSSR_BH** — Call Setup Success Rate measured during the Busy Hour (peak traffic period). Most critical KPI for voice quality assessment.",
    "availability": "**Network Availability (tch_availability_daily)** — Percentage of time a cell is operational. Formula: Available Hours / Total Hours × 100. **Higher is better.** Target: ≥ 99%.",
    "drop rate": "**TCH Drop Rate** — Share of established calls that drop before the subscriber ends them. Formula used here: TCH Drops / TCH Assignments × 100. **Lower is better.** Threshold: ≤ 2%. Available for 2G and 3G.",
    "tch_drops": "**TCH Drops** — Number of established calls that were dropped (a count, not a percentage). The rate is TCH Drops / TCH Assignments × 100. **Lower is better.**",
    "tch_drops_bh": "**TCH Drops BH** — Number of dropped calls during the Busy Hour. Key indicator of radio link quality and coverage issues.",
    "data_sr": "**Data Success Rate** — Percentage of successful data (PS) sessions. **Higher is better.** Indicates data network quality. Available for 2G, 3G, 4G and 5G.",
    "data_sr_bh": "**Data SR BH** — Data Success Rate during Busy Hour. Critical for measuring user data experience at peak times.",
    "ultilization": "**Cell Utilization** — Percentage of available cell capacity being used. High utilization (>80%) indicates congestion risk.",
    "utilization": "**Cell Utilization** — Same as ultilization. Percentage of available cell capacity being used. High values indicate need for capacity expansion.",
    "dl_volume": "**DL Volume** — Total data downloaded by users (Downlink Volume). Indicates data demand.",
    "cs_traffic": "**CS Traffic** — Circuit Switched Traffic, measured in Erlangs. Represents voice traffic load on the network.",
    "erlang": "**Erlang** — Unit of telephone traffic: 1 Erlang = one channel busy for one hour. CS Traffic is measured in Erlangs.",
    "vendor": "In this system, **Vendor** refers to the network equipment manufacturer (e.g., Huawei, Ericsson, Nokia) for each cell or site.",
    "bh": "**BH (Busy Hour)** — The one-hour period in a day with the highest traffic load. KPIs measured during BH represent worst-case network conditions.",
    "busy hour": "**Busy Hour (BH)** — The one-hour period in a day with the highest traffic load. KPIs measured during BH represent worst-case network conditions.",
    "kpi": "**KPI (Key Performance Indicator)** — Metrics used to evaluate telecom network performance. Main KPIs: CSSR, Availability, TCH Drop Rate, Data SR, CS Traffic, DL/UL Volume.",
}


# Technology KPIs that answer the same question as a daily-dashboard KPI ("3G call drop rate" -> 3G CDR).
_TECH_CONCEPTS = {
    "umts_voice_cssr": "cssr_bh", "umts_voice_cdr": "drop_rate_bh", "umts_data_setup_sr": "data_sr_bh",
    "umts_availability": "tch_availability_daily", "umts_cs_traffic": "cs_traffic_daily", "umts_data_volume": "total_volume_daily",
    "gsm_tch_drop_rate": "drop_rate_bh", "gsm_data_setup_sr": "data_sr_bh", "gsm_cs_traffic": "cs_traffic_daily",
    "gsm_data_volume": "total_volume_daily", "lte_cssr": "cssr_bh", "lte_erab_drop": "drop_rate_bh",
    "lte_availability": "tch_availability_daily", "lte_dl_volume": "dl_volume_daily", "lte_ul_volume": "ul_volume_daily",
    "nr_availability": "tch_availability_daily",
}
import dataclasses as _dc  # noqa: E402
for _mid, _concept in _TECH_CONCEPTS.items():
    METRICS[_mid] = _dc.replace(METRICS[_mid], concept=_concept)
