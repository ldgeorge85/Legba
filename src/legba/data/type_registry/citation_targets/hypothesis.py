# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Citation target ``hypothesis``."""

from legba.data.type_registry import declare


TYPE = declare(
    family="citation_target",
    id="hypothesis",
    table="hypotheses",
    column="hypothesis_id",
    member_fields={"open_questions": "question_ids"},
    evidence="background",
    doc="A standing open question (a hypotheses row), as a member of the open-questions block.",
)
