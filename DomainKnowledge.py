"""
Domain Knowledge Base for Stereotech FDM Materials and FDM Physics.
Source of truth: https://wiki.stereotech.org/ru/fdm-materials
"""

class DomainKnowledge:
    """Structured engineering knowledge on materials, chemistry, and print physics."""

    MATERIALS = {
        # 1. Proto
        "proto_pla": {
            "name": "Proto PLA",
            "family": "Proto",
            "category": "Макетный",
            "tg_c": 55,
            "t_max_service_c": 50,
            "tensile_mpa": 50,
            "modulus_gpa": 3.5,
            "elongation_pct": 3.0,
            "filler": None,
            "dielectric": True,
            "esd_safe": False,
            "chem_resistance": ["water_cold"],
            "friction_pair_pom": False,
            "features": "Жесткий, хрупкий на излом, низкая термостойкость. Нестоек к УФ и влаге.",
            "applications": "Макеты, прототипы, сувениры, ненагруженные концепты."
        },
        "proto_pva": {
            "name": "Proto PVA",
            "family": "Proto",
            "category": "Растворимый материал поддержек",
            "tg_c": 50,
            "t_max_service_c": 45,
            "tensile_mpa": 30,
            "modulus_gpa": 2.0,
            "elongation_pct": 5.0,
            "filler": None,
            "dielectric": True,
            "esd_safe": False,
            "chem_resistance": [],
            "friction_pair_pom": False,
            "features": "Полностью растворяется в воде. Идеален для поддержек закрытых полостей.",
            "applications": "Водорастворимые поддержки для сложных внутренних каналов."
        },

        # 2. Enduse
        "enduse_petg": {
            "name": "Enduse PETG",
            "family": "Enduse",
            "category": "Конструкционный термопласт",
            "tg_c": 75,
            "t_max_service_c": 70,
            "tensile_mpa": 45,
            "modulus_gpa": 2.1,
            "elongation_pct": 20.0,
            "filler": None,
            "dielectric": True,
            "esd_safe": False,
            "chem_resistance": ["uv", "alcohols", "alkalis_mild", "water"],
            "friction_pair_pom": False,
            "features": "Высокая вязкость, ударопрочность, УФ-стабильность. Тонкие элементы не отламываются.",
            "applications": "Декоративные фигурки на солнце, корпуса приборов, плафоны, тара."
        },
        "enduse_pa6": {
            "name": "Enduse PA6",
            "family": "Enduse",
            "category": "Антифрикционный конструкционный полиамид",
            "tg_c": 60,
            "t_max_service_c": 120,
            "tensile_mpa": 65,
            "modulus_gpa": 2.3,
            "elongation_pct": 50.0,
            "filler": None,
            "dielectric": True,
            "esd_safe": False,
            "chem_resistance": ["oils", "greases", "fuels", "alkalis_ph10_11", "hot_glue_160c"],
            "friction_pair_pom": True,
            "features": "Низкий коэффициент трения, износостойкость. ИДЕАЛЬНАЯ ПАРА ДЛЯ POM (полиацеталя).",
            "applications": "Копирные валы, направляющие, подшипники скольжения, шестерни без абразивного износа."
        },
        "enduse_pp": {
            "name": "Enduse PP",
            "family": "Enduse",
            "category": "Химостойкий полиолефин",
            "tg_c": -10,
            "t_max_service_c": 95,
            "tensile_mpa": 28,
            "modulus_gpa": 1.2,
            "elongation_pct": 200.0,
            "filler": None,
            "dielectric": True,
            "esd_safe": False,
            "chem_resistance": ["acids_concentrated", "alkalis_concentrated", "water_boiling"],
            "friction_pair_pom": False,
            "features": "Исключительная химическая стойкость, гидрофобность, гигиеничность.",
            "applications": "Емкости под химикаты, фитинги агрессивных сред, лабораторная посуда."
        },
        "enduse_abs": {
            "name": "Enduse ABS",
            "family": "Enduse",
            "category": "Ударопрочный термопласт",
            "tg_c": 105,
            "t_max_service_c": 90,
            "tensile_mpa": 42,
            "modulus_gpa": 2.2,
            "elongation_pct": 15.0,
            "filler": None,
            "dielectric": True,
            "esd_safe": False,
            "chem_resistance": ["oils", "alkalis_mild"],
            "friction_pair_pom": False,
            "features": "Ударопрочность, полировка ацетоном, термостойкость до +90°C.",
            "applications": "Корпуса электроники, панели приборов, защитные крышки."
        },

        # 3. Fiberpart
        "fiberpart_pp_g30": {
            "name": "Fiberpart PP G30",
            "family": "Fiberpart",
            "category": "Стеклонаполненный полипропилен (30% GF)",
            "tg_c": 0,
            "t_max_service_c": 105,
            "tensile_mpa": 70,
            "modulus_gpa": 5.5,
            "elongation_pct": 3.5,
            "filler": "Glass 30%",
            "dielectric": True,
            "esd_safe": False,
            "chem_resistance": ["glycol_hot", "alkalis", "hot_water", "salts"],
            "friction_pair_pom": False,  # АБРАЗИВ!
            "features": "Эталонная химстойкость при +85°C под давлением. Жесткий диэлектрик.",
            "applications": "Крыльчатки циркуляционных насосов (гликоль, щелочь 16 бар), корпусы фильтров."
        },
        "fiberpart_pa12_g12": {
            "name": "Fiberpart PA12 G12",
            "family": "Fiberpart",
            "category": "Стеклонаполненный полиамид 12 (12% GF)",
            "tg_c": 50,
            "t_max_service_c": 95,
            "tensile_mpa": 75,
            "modulus_gpa": 3.8,
            "elongation_pct": 5.0,
            "filler": "Glass 12%",
            "dielectric": True,
            "esd_safe": False,
            "chem_resistance": ["oils", "ozone", "fuels", "low_moisture_absorption"],
            "friction_pair_pom": False,
            "features": "Минимальное влагопоглощение среди нейлонов, малый вес (1.05 г/см³).",
            "applications": "Высокоскоростные вентиляторы (до 3000 об/мин), детали авиамоделей."
        },
        "fiberpart_pc_g20": {
            "name": "Fiberpart PC G20",
            "family": "Fiberpart",
            "category": "Стеклонаполненный поликарбонат (20% GF)",
            "tg_c": 145,
            "t_max_service_c": 130,
            "tensile_mpa": 95,
            "modulus_gpa": 6.2,
            "elongation_pct": 2.5,
            "filler": "Glass 20%",
            "dielectric": True,
            "esd_safe": False,
            "chem_resistance": ["oils", "moisture_98pct", "mild_acids"],
            "friction_pair_pom": False,
            "features": "Сверхпрочный композит, высокая теплостойкость. Выдерживает моменты до 75 Н·м.",
            "applications": "Силовые шестерни редукторов, кронштейны, нагруженные крепежи."
        },
        "fiberpart_pa6_c25": {
            "name": "Fiberpart PA6 C25",
            "family": "Fiberpart",
            "category": "Угленаполненный полиамид 6 (25% CF)",
            "tg_c": 65,
            "t_max_service_c": 140,
            "tensile_mpa": 130,
            "modulus_gpa": 9.5,
            "elongation_pct": 2.0,
            "filler": "Carbon 25%",
            "dielectric": False,
            "esd_safe": True,
            "chem_resistance": ["fuels", "oils", "hydrocarbons"],
            "friction_pair_pom": False,
            "features": "Электропроводность (ESD защита), максимальная жесткость на изгиб.",
            "applications": "Беспилотники, робототехника, кронштейны электроники с защитой от статики."
        },

        # 4. Sealant
        "sealant_tpu_93": {
            "name": "Sealant TPU 93",
            "family": "Sealant",
            "category": "Маслобензостойкий эластомер",
            "tg_c": -40,
            "t_max_service_c": 80,
            "tensile_mpa": 35,
            "modulus_gpa": 0.1,
            "elongation_pct": 450.0,
            "filler": None,
            "dielectric": True,
            "esd_safe": False,
            "chem_resistance": ["mos2_grease", "oils", "fuels", "deicers", "road_salts"],
            "friction_pair_pom": False,
            "features": "Удлинение до 450%, устойчивость к знакопеременным изгибам до 1000 об/мин.",
            "applications": "Пыльники ШРУС, сильфоны подвески, мембраны, защитные чехлы."
        },
        "sealant_tpu_a70": {
            "name": "Sealant TPU A70",
            "family": "Sealant",
            "category": "Мягкий высокоэластичный полиуретан",
            "tg_c": -45,
            "t_max_service_c": 75,
            "tensile_mpa": 25,
            "modulus_gpa": 0.05,
            "elongation_pct": 550.0,
            "filler": None,
            "dielectric": True,
            "esd_safe": False,
            "chem_resistance": ["water", "mild_chemicals", "greases"],
            "friction_pair_pom": False,
            "features": "Максимальная мягкость и демпфирование.",
            "applications": "Уплотнительные кольца, мягкие виброопоры, вакуумные присоски."
        },

        # 5. Hightemp
        "hightemp_peek": {
            "name": "Hightemp PEEK",
            "family": "Hightemp",
            "category": "Суперконструкционный полиэфирэфиркетон",
            "tg_c": 143,
            "t_max_service_c": 250,
            "tensile_mpa": 100,
            "modulus_gpa": 4.0,
            "elongation_pct": 10.0,
            "filler": None,
            "dielectric": True,
            "esd_safe": False,
            "chem_resistance": ["aviation_fuel", "acids_strong", "steam", "autoclave"],
            "friction_pair_pom": False,
            "features": "Экстремальная термостойкость до +250°C, автоклавирование, авиационный класс.",
            "applications": "Нефтегаз, авиакосмос, медицинские автоклавируемые инструменты."
        },
        "hightemp_pei": {
            "name": "Hightemp PEI",
            "family": "Hightemp",
            "category": "Полиэфиримид (Ultem)",
            "tg_c": 217,
            "t_max_service_c": 170,
            "tensile_mpa": 85,
            "modulus_gpa": 3.2,
            "elongation_pct": 8.0,
            "filler": None,
            "dielectric": True,
            "esd_safe": False,
            "chem_resistance": ["fuels", "hydrocarbons", "flame_ul94_v0"],
            "friction_pair_pom": False,
            "features": "Самозатухающий (UL94 V-0), дымобезопасность по стандартам FAR 25.853.",
            "applications": "Интерьеры самолетов, высоковольтные изоляторы, железнодорожный транспорт."
        }
    }

    @classmethod
    def get_compact_summary(cls):
        """Returns a dense, high-signal reference string for LLM context."""
        lines = [
            "ИНЖЕНЕРНЫЙ СПРАВОЧНИК МАТЕРИАЛОВ STEREOTECH (wiki.stereotech.org):",
            "1. PROTO: Proto PLA (макеты до +50°C), Proto PVA (водорастворимые поддержки).",
            "2. ENDUSE: PETG (вязкий, УФ, +70°C), PA6 (антифрикционный нейлон, ТОЛЬКО ОН для пар трения с POM!), PP (кислоты/щелочи), ABS (ударопрочный), TPU D60/D70 (жесткие демпферы).",
            "3. FIBERPART (GF/CF - волокно работает как наждак! В ПАРЫ ТРЕНИЯ С POM/БРОНЗОЙ СТАВИТЬ ЗАПРЕЩЕНО):",
            "   - Glass (диэлектрики): PP G30 (насосы, гликоль +85°C), PA12 G12 (легкий 1.05 г/см³, вентиляторы 3000 об/мин), PC G20 (рекордная прочность, шестерни до 75 Н·м).",
            "   - Carbon (антистатика ESD): PA6 C25 (рекордная жесткость, малый вес).",
            "4. SEALANT: TPU 93 / A90 / A70 (эластомеры >400%, стойки к MoS2 смазкам, солям, изгибам 1000 об/мин: пыльники ШРУС, мембраны, гофры).",
            "5. HIGHTEMP: PEEK (+250°C, автоклав, авиатопливо), PEI Ultem (+170°C, самозатухающий UL94 V-0), PPS G40 (перегретый пар +200°C)."
        ]
        return "\n".join(lines)
