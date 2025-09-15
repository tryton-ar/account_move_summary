# This file is part of the account_move_summary module for Tryton.
# The COPYRIGHT file at the top level of this repository contains
# the full copyright notices and license terms.

from trytond.pool import Pool
from . import account
from . import move


def register():
    Pool.register(
        account.Period,
        account.FiscalYear,
        move.Summary,
        move.SummaryPeriod,
        move.SummaryMove,
        move.SummaryLine,
        move.Move,
        move.RenumberSummaryMovesStart,
        move.PrintSummaryGeneralJournalStart,
        module='account_move_summary', type_='model')
    Pool.register(
        account.RenewFiscalYear,
        move.RenumberSummaryMoves,
        move.PrintSummaryGeneralJournal,
        module='account_move_summary', type_='wizard')
    Pool.register(
        move.SummaryGeneralJournalPDF,
        move.SummaryGeneralJournalXLS,
        module='account_move_summary', type_='report')
