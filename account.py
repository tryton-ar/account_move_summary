# This file is part of the account_move_summary module for Tryton.
# The COPYRIGHT file at the top level of this repository contains
# the full copyright notices and license terms.
from trytond.model import fields
from trytond.pool import Pool, PoolMeta
from trytond.model.exceptions import ValidationError
from trytond.pyson import Eval, Id
from trytond.i18n import gettext


class FiscalYear(metaclass=PoolMeta):
    'Fiscal Year'
    __name__ = 'account.fiscalyear'

    post_summary_move_sequence = fields.Many2One(
        'ir.sequence', "Post Summary Move Sequence", required=True,
        domain=[
            ('sequence_type', '=',
                Id('account', 'sequence_type_account_move')),
            ('company', '=', Eval('company')),
            ],
        depends=['company'])


class Period(metaclass=PoolMeta):
    __name__ = 'account.period'

    post_summary_move_sequence = fields.Many2One('ir.sequence',
        'Post Summary Move Sequence',
        domain=[
            ('sequence_type', '=',
                Id('account', 'sequence_type_account_move')),
            ['OR',
                ('company', '=', None),
                ('company', '=', Eval('company', -1)),
                ],
            ],
        depends=['company'])

    @classmethod
    def create(cls, vlist):
        FiscalYear = Pool().get('account.fiscalyear')
        vlist = [x.copy() for x in vlist]
        for vals in vlist:
            if vals.get('fiscalyear'):
                fiscalyear = FiscalYear(vals['fiscalyear'])
                if not vals.get('post_summary_move_sequence'):
                    vals['post_summary_move_sequence'] = (
                        fiscalyear.post_summary_move_sequence.id)
        return super(Period, cls).create(vlist)

    @classmethod
    def write(cls, *args):
        SummaryMove = Pool().get('account.summary.move')
        actions = iter(args)
        args = []
        for periods, values in zip(actions, actions):
            if values.get('post_summary_move_sequence'):
                for period in periods:
                    if (period.post_summary_move_sequence
                            and period.post_summary_move_sequence.id
                            != values['post_summary_move_sequence']):
                        if SummaryMove.search([
                                    ('period', '=', period.id),
                                    ('state', '=', 'posted'),
                                    ]):
                            raise ValidationError(
                                gettext('account_move_summary'
                                    '.msg_change_period_post_move_sequence',
                                    period=period.rec_name))
            args.extend((periods, values))
        super(Period, cls).write(*args)

    @property
    def post_summary_move_sequence_used(self):
        return self.post_summary_move_sequence or \
            self.fiscalyear.post_summary_move_sequence


class RenewFiscalYear(metaclass=PoolMeta):
    __name__ = 'account.fiscalyear.renew'

    def fiscalyear_defaults(self):
        pool = Pool()
        Sequence = pool.get('ir.sequence')

        defaults = super(RenewFiscalYear, self).fiscalyear_defaults()
        previous_sequence = \
            self.start.previous_fiscalyear.post_summary_move_sequence
        sequence, = Sequence.copy([previous_sequence],
            default={
                'name': lambda data: data['name'].replace(
                    self.start.previous_fiscalyear.name,
                    self.start.name)
                })
        if self.start.reset_sequences:
            sequence.number_next = 1
        else:
            sequence.number_next = previous_sequence.number_next
        sequence.save()
        defaults['post_summary_move_sequence'] = sequence.id
        return defaults
