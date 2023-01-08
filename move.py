# This file is part of the account_move_summary module for Tryton.
# The COPYRIGHT file at the top level of this repository contains
# the full copyright notices and license terms.
from decimal import Decimal
from functools import reduce
from itertools import groupby
from operator import itemgetter
from sql.aggregate import Sum
from sql.functions import CharLength

from trytond.model import ModelView, ModelSQL, Workflow, fields
from trytond.wizard import Wizard, StateView, StateAction, Button
from trytond.pool import Pool, PoolMeta
from trytond.pyson import PYSONEncoder, Eval, Bool, If
from trytond.report import Report
from trytond.transaction import Transaction
from trytond.tools import reduce_ids, grouped_slice

_MOVE_STATES = {
    'readonly': Eval('state') == 'posted',
    }
_MOVE_DEPENDS = ['state']
_LINE_STATES = {
    'readonly': Eval('move_state') == 'posted',
    }
_LINE_DEPENDS = ['move_state']


class Summary(Workflow, ModelSQL, ModelView):
    'Summary'
    __name__ = 'account.summary'

    _states = {'readonly': Eval('state') != 'draft'}
    _depends = ['state']

    name = fields.Char('Name', required=True,
        states=_states, depends=_depends)
    company = fields.Many2One('company.company', 'Company', required=True,
        states=_states, depends=_depends)
    date = fields.Date('Date', required=True,
        states=_states, depends=_depends)
    periods = fields.Many2Many('account.summary.period',
        'summary', 'period', 'Periods', required=True,
        domain=[('company', '=', Eval('company', -1))],
        states=_states, depends=_depends + ['company'])
    state = fields.Selection([
        ('draft', 'Draft'),
        ('calculated', 'Calculated'),
        ('posted', 'Posted'),
        ], 'State', required=True, readonly=True, select=True)

    @classmethod
    def __setup__(cls):
        super().__setup__()
        cls._order.insert(0, ('date', 'DESC'))
        cls._order.insert(1, ('id', 'DESC'))
        cls._transitions |= set((
            ('draft', 'calculated'),
            ('calculated', 'draft'),
            ('calculated', 'posted'),
            ))
        cls._buttons.update({
            'draft': {
                'invisible': Eval('state') != 'calculated',
                'depends': ['state'],
                },
            'compute': {
                'invisible': Eval('state') != 'draft',
                'depends': ['state'],
                },
            'post': {
                'invisible': Eval('state') != 'calculated',
                'depends': ['state'],
                },
            })
        cls._error_messages.update({
            'msg_delete_posted_summary':
                'Summary "%(summary)s" in calculated or posted state '
                'can not be deleted.',
            })

    @staticmethod
    def default_state():
        return 'draft'

    @staticmethod
    def default_company():
        return Transaction().context.get('company')

    @staticmethod
    def default_date():
        Date = Pool().get('ir.date')
        return Date.today()

    @classmethod
    @ModelView.button
    @Workflow.transition('draft')
    def draft(cls, summaries):
        for summary in summaries:
            summary._draft_summary()

    def _draft_summary(self):
        SummaryMove = Pool().get('account.summary.move')
        moves = SummaryMove.search([('summary', '=', self.id)])
        SummaryMove.delete([m for m in moves])

    @classmethod
    @ModelView.button
    @Workflow.transition('calculated')
    def compute(cls, summaries):
        for summary in summaries:
            summary._validate_summary()
            summary._compute_summary()

    def _validate_summary(self):
        pass

    def _compute_summary(self):
        pool = Pool()
        Move = pool.get('account.move')
        SummaryMove = pool.get('account.summary.move')
        SummaryMoveLine = pool.get('account.summary.move.line')
        Model = pool.get('ir.model')

        for period in self.periods:
            accum = {}
            moves = Move.search([
                ('company', '=', self.company.id),
                ('period', '=', period.id),
                ('state', '=', 'posted'),
                ('summary_move', '=', None),
                ])
            for move in moves:
                type_ = None
                origin = str(move.origin).split(',')[0] \
                    if move.origin else None
                if origin:
                    type_ = '%s_%s' % (
                        origin.replace('.', '_'), str(move.journal.id))
                    if type_ not in accum:
                        accum[type_] = {
                            'moves': [],
                            'model': origin,
                            'journal': move.journal
                            }
                else:
                    type_ = 'single_move_%s' % str(move.id)
                    accum[type_] = {
                        'moves': [],
                        'journal': move.journal,
                        'description': move.description
                        }
                accum[type_]['moves'].append(move.id)
                for l in move.lines:
                    data = accum[type_].get(l.account.id, {
                        'debit': Decimal('0.0'),
                        'credit': Decimal('0.0'),
                        'description': None
                        })
                    data['debit'] += l.debit
                    data['credit'] += l.credit
                    if type_[0:11] == 'single_move':
                        data['description'] = l.description
                    else:
                        data['description'] = l.account.name
                    accum[type_][l.account.id] = data

            for keys, values in accum.items():
                if not values:
                    continue
                summary_move_lines = []
                for account_id, value in values.items():
                    # Ignore move data
                    if account_id in [
                            'moves', 'model', 'journal', 'description']:
                        continue
                    summary_move_lines.append(SummaryMoveLine(
                        account=account_id,
                        debit=value['debit'],
                        credit=value['credit'],
                        description=value['description'],
                        date=period.end_date,
                        ))

                description = None
                origin_name = None
                if 'model' in values:
                    model = Model.search([
                        ('model', '=', values['model']),
                        ])
                    if model:
                        origin_name = model[0].name
                    description = '%s - %s' % (
                        origin_name, values['journal'].name)
                elif 'description' in values:
                    description = values['description']
                summary_move = SummaryMove()
                summary_move.journal = values['journal']
                summary_move.description = description
                summary_move.period = period
                summary_move.company = self.company
                summary_move.summary = self
                summary_move.lines = summary_move_lines
                summary_move.save()

                summarized_moves = Move.browse(values['moves'])
                Move.write(summarized_moves, {
                    'summary_move': summary_move.id})

    @classmethod
    @ModelView.button
    @Workflow.transition('posted')
    def post(cls, summaries):
        for summary in summaries:
            summary._post_summary()

    def _post_summary(self):
        SummaryMove = Pool().get('account.summary.move')
        moves = SummaryMove.search([('summary', '=', self.id)],
            order=[('date', 'ASC')])
        SummaryMove.post([m for m in moves])

    @classmethod
    def delete(cls, summaries):
        for summary in summaries:
            if summary.state in ['calculated', 'posted']:
                cls.raise_user_error('msg_delete_posted_summary', {
                        'summary': summary.rec_name,
                        })
        super(Summary, cls).delete(summaries)


class SummaryPeriod(ModelSQL):
    'Summary - Period'
    __name__ = 'account.summary.period'

    summary = fields.Many2One('account.summary',
        'Summary', ondelete='CASCADE', select=True, required=True)
    period = fields.Many2One('account.period', 'Period', ondelete='CASCADE',
        select=True, required=True)


class SummaryMove(ModelSQL, ModelView):
    'Summary Move'
    __name__ = 'account.summary.move'
    _rec_name = 'number'

    number = fields.Char('Number', readonly=True)
    post_number = fields.Char('Post Number', readonly=True,
        help='Also known as Folio Number.')
    company = fields.Many2One('company.company', 'Company', required=True,
        states=_MOVE_STATES, depends=_MOVE_DEPENDS)
    journal = fields.Many2One('account.journal', 'Journal', required=True,
        states=_MOVE_STATES, depends=_MOVE_DEPENDS)
    period = fields.Many2One('account.period', 'Period', required=True,
        domain=[
            ('company', '=', Eval('company', -1)),
            If(Eval('state') == 'draft',
                ('state', '=', 'open'),
                ()),
            ],
        states=_MOVE_STATES, depends=_MOVE_DEPENDS + ['company', 'state'],
        select=True)
    date = fields.Date('Effective Date', select=True,
        states=_MOVE_STATES, depends=_MOVE_DEPENDS)
    post_date = fields.Date('Post Date', readonly=True)
    description = fields.Char('Description', states=_MOVE_STATES,
        depends=_MOVE_DEPENDS)
    summary = fields.Many2One('account.summary',
        'Summary', readonly=True, depends=['company'],
        domain=[
            ('company', '=', Eval('company', -1)),
            ])
    state = fields.Selection([
        ('draft', 'Draft'),
        ('posted', 'Posted'),
        ], 'State', required=True, readonly=True, select=True)
    lines = fields.One2Many('account.summary.move.line', 'move', 'Lines',
        states=_MOVE_STATES, depends=_MOVE_DEPENDS + ['company'],
        context={
            'period': Eval('period'),
            'date': Eval('date'),
            })

    @classmethod
    def __setup__(cls):
        super(SummaryMove, cls).__setup__()
        cls._check_modify_exclude = ['post_number', 'lines']
        cls._order.insert(0, ('date', 'DESC'))
        cls._order.insert(1, ('number', 'DESC'))

    @classmethod
    def order_post_number(cls, tables):
        table, _ = tables[None]
        return [CharLength(table.post_number), table.post_number]

    @staticmethod
    def default_company():
        return Transaction().context.get('company')

    @staticmethod
    def default_state():
        return 'draft'

    @classmethod
    def search_rec_name(cls, name, clause):
        if clause[1].startswith('!') or clause[1].startswith('not '):
            bool_op = 'AND'
        else:
            bool_op = 'OR'
        return [bool_op,
            ('post_number',) + tuple(clause[1:]),
            (cls._rec_name,) + tuple(clause[1:]),
            ]

    @classmethod
    def create(cls, vlist):
        pool = Pool()
        Sequence = pool.get('ir.sequence')
        Journal = pool.get('account.journal')

        vlist = [x.copy() for x in vlist]
        for vals in vlist:
            if not vals.get('number'):
                journal_id = (vals.get('journal')
                        or Transaction().context.get('journal'))
                if journal_id:
                    journal = Journal(journal_id)
                    vals['number'] = Sequence.get_id(journal.sequence.id)

        moves = super(SummaryMove, cls).create(vlist)
        cls.validate_move(moves)
        return moves

    @classmethod
    def validate_move(cls, moves):
        '''
        Validate balanced move
        '''
        pool = Pool()
        SummaryMoveLine = pool.get('account.summary.move.line')
        line = SummaryMoveLine.__table__()

        cursor = Transaction().connection.cursor()

        amounts = {}
        move2draft_lines = {}
        for sub_move_ids in grouped_slice([m.id for m in moves]):
            red_sql = reduce_ids(line.move, sub_move_ids)

            cursor.execute(*line.select(line.move,
                    Sum(line.debit - line.credit),
                    where=red_sql,
                    group_by=line.move))
            amounts.update(dict(cursor))

            cursor.execute(*line.select(line.move, line.id,
                    where=red_sql & (line.state == 'draft'),
                    order_by=line.move))
            move2draft_lines.update(dict((k, [j[1] for j in g])
                    for k, g in groupby(cursor, itemgetter(0))))

        valid_moves = []
        draft_moves = []
        for move in moves:
            if move.id not in amounts:
                continue
            amount = amounts[move.id]
            # SQLite uses float for SUM
            if not isinstance(amount, Decimal):
                amount = Decimal(amount)
            draft_lines = SummaryMoveLine.browse(
                move2draft_lines.get(move.id, []))
            if not move.company.currency.is_zero(amount):
                draft_moves.append(move.id)
                continue
            if not draft_lines:
                continue
            valid_moves.append(move.id)
        for move_ids, state in (
                (valid_moves, 'valid'),
                (draft_moves, 'draft'),
                ):
            if move_ids:
                for sub_ids in grouped_slice(move_ids):
                    red_sql = reduce_ids(line.move, sub_ids)
                    # Use SQL to prevent double validate loop
                    cursor.execute(*line.update(
                            columns=[line.state],
                            values=[state],
                            where=red_sql))

    @classmethod
    def post(cls, moves):
        pool = Pool()
        Sequence = pool.get('ir.sequence')

        for move in moves:
            move.state = 'posted'
            if not move.post_number:
                move.post_date = move.date
                move.post_number = Sequence.get_id(
                    move.period.post_summary_move_sequence_used.id)
        cls.save(moves)


class SummaryLine(ModelSQL, ModelView):
    'Summary Move Line'
    __name__ = 'account.summary.move.line'

    debit = fields.Numeric('Debit', digits=(16, Eval('currency_digits', 2)),
        required=True, states=_LINE_STATES,
        depends=[
            'currency_digits', 'credit', 'tax_lines', 'journal'
            ] + _LINE_DEPENDS)
    credit = fields.Numeric('Credit', digits=(16, Eval('currency_digits', 2)),
        required=True, states=_LINE_STATES,
        depends=[
            'currency_digits', 'debit', 'tax_lines', 'journal'
            ] + _LINE_DEPENDS)
    account = fields.Many2One('account.account', 'Account', required=True,
        domain=[
            ('company', '=', Eval('company', -1)),
            ('type', '!=', None),
            ],
        context={
            'company': Eval('company', -1),
            },
        select=True, states=_LINE_STATES,
        depends=_LINE_DEPENDS + ['date', 'company'])
    move = fields.Many2One('account.summary.move', 'Move', select=True,
        required=True, ondelete='CASCADE',
        states={
            'required': False,
            'readonly': (
                ((Eval('state') == 'valid') | _LINE_STATES['readonly'])
                & Bool(Eval('move'))),
            },
        depends=['state'] + _LINE_DEPENDS)
    period = fields.Function(fields.Many2One('account.period', 'Period',
            states=_LINE_STATES, depends=_LINE_DEPENDS),
            'get_move_field', setter='set_move_field',
            searcher='search_move_field')
    company = fields.Function(fields.Many2One(
            'company.company', "Company", states=_LINE_STATES,
            depends=_LINE_DEPENDS),
            'get_move_field', setter='set_move_field',
            searcher='search_move_field')
    date = fields.Function(fields.Date('Effective Date', required=True,
            states=_LINE_STATES, depends=_LINE_DEPENDS),
            'on_change_with_date', setter='set_move_field',
            searcher='search_move_field')
    description = fields.Char('Description', states=_LINE_STATES,
            depends=_LINE_DEPENDS)
    move_description = fields.Function(fields.Char('Move Description',
            states=_LINE_STATES, depends=_LINE_DEPENDS),
        'get_move_field', setter='set_move_field',
        searcher='search_move_field')
    amount_second_currency = fields.Numeric('Amount Second Currency',
        digits=(16, Eval('second_currency_digits', 2)),
        help='The amount expressed in a second currency.',
        states={
            'required': Bool(Eval('second_currency')),
            'readonly': _LINE_STATES['readonly'],
            },
        depends=['second_currency_digits', 'second_currency'] + _LINE_DEPENDS)
    second_currency = fields.Many2One('currency.currency', 'Second Currency',
            help='The second currency.',
        domain=[
            If(~Eval('second_currency_required'),
                (),
                ('id', '=', Eval('second_currency_required', -1))),
            ],
        states={
            'required': (Bool(Eval('amount_second_currency'))
                | Bool(Eval('second_currency_required'))),
            'readonly': _LINE_STATES['readonly']
            },
        depends=['amount_second_currency',
            'second_currency_required'] + _LINE_DEPENDS)
    second_currency_required = fields.Function(
        fields.Many2One('currency.currency', "Second Currency Required"),
        'on_change_with_second_currency_required')
    state = fields.Selection([
        ('draft', 'Draft'),
        ('valid', 'Valid'),
        ], 'State', readonly=True, required=True, select=True)
    move_state = fields.Function(
        fields.Selection('get_move_states', "Move State"),
        'on_change_with_move_state', searcher='search_move_field')
    currency = fields.Function(fields.Many2One(
            'currency.currency', "Currency"), 'on_change_with_currency')
    currency_digits = fields.Function(fields.Integer('Currency Digits'),
            'on_change_with_currency_digits')
    second_currency_digits = fields.Function(fields.Integer(
        'Second Currency Digits'), 'on_change_with_second_currency_digits')
    amount = fields.Function(fields.Numeric('Amount',
            digits=(16, Eval('amount_currency_digits', 2)),
            depends=['amount_currency_digits']),
        'get_amount')
    amount_currency = fields.Function(fields.Many2One('currency.currency',
            'Amount Currency'), 'get_amount_currency')
    amount_currency_digits = fields.Function(fields.Integer(
            'Amount Currency Digits'), 'get_amount_currency')

    @classmethod
    def default_company(cls):
        return Transaction().context.get('company')

    @staticmethod
    def default_state():
        return 'draft'

    @fields.depends('account')
    def on_change_with_currency(self, name=None):
        if self.account:
            return self.account.currency.id

    @fields.depends('account')
    def on_change_with_currency_digits(self, name=None):
        if self.account:
            return self.account.currency_digits
        else:
            return 2

    @fields.depends('second_currency')
    def on_change_with_second_currency_digits(self, name=None):
        if self.second_currency:
            return self.second_currency.digits
        else:
            return 2

    @fields.depends('account')
    def on_change_with_second_currency_required(self, name=None):
        if self.account and self.account.second_currency:
            return self.account.second_currency.id

    def get_move_field(self, name):
        field = getattr(self.__class__, name)
        if name.startswith('move_'):
            name = name[5:]
        value = getattr(self.move, name)
        if isinstance(value, ModelSQL):
            if field._type == 'reference':
                return str(value)
            return value.id
        return value

    @fields.depends('move', '_parent_move.date')
    def on_change_with_date(self, name=None):
        if self.move:
            return self.move.date

    @classmethod
    def set_move_field(cls, lines, name, value):
        if name.startswith('move_'):
            name = name[5:]
        if not value:
            return
        Move = Pool().get('account.summary.move')
        Move.write([line.move for line in lines], {
                name: value,
                })

    @classmethod
    def search_move_field(cls, name, clause):
        nested = clause[0].lstrip(name)
        if name.startswith('move_'):
            name = name[5:]
        return [('move.' + name + nested,) + tuple(clause[1:])]

    @classmethod
    def get_move_states(cls):
        pool = Pool()
        Move = pool.get('account.summary.move')
        return Move.fields_get(['state'])['state']['selection']

    @fields.depends('move', '_parent_move.state')
    def on_change_with_move_state(self, name=None):
        if self.move:
            return self.move.state

    def get_amount(self, name):
        sign = -1 if self.account.type.statement == 'income' else 1
        if self.amount_second_currency is not None:
            return self.amount_second_currency * sign
        else:
            return (self.debit - self.credit) * sign

    def get_amount_currency(self, name):
        if self.second_currency:
            currency = self.second_currency
        else:
            currency = self.account.currency
        if name == 'amount_currency':
            return currency.id
        elif name == 'amount_currency_digits':
            return currency.digits

    def get_rec_name(self, name):
        if self.debit > self.credit:
            return self.account.rec_name
        else:
            return '(%s)' % self.account.rec_name

    @classmethod
    def search_rec_name(cls, name, clause):
        return [('account.rec_name',) + tuple(clause[1:])]


class Move(metaclass=PoolMeta):
    __name__ = 'account.move'

    summary_move = fields.Many2One('account.summary.move',
        'Summary Move', help="The related summarized move.",
        readonly=True, depends=['company'],
        domain=[
            ('company', '=', Eval('company', -1)),
            ])

    @classmethod
    def __setup__(cls):
        super(Move, cls).__setup__()
        cls._check_modify_exclude.append('summary_move')


class RenumberSummaryMovesStart(ModelView):
    '''Renumber Summary Account Moves Start'''
    __name__ = 'account.summary.move.renumber.start'

    fiscalyear = fields.Many2One('account.fiscalyear', 'Fiscal Year',
        required=True)
    first_number = fields.Integer('First Number', required=True,
        domain=[('first_number', '>', 0)])
    first_move = fields.Many2One('account.summary.move', 'First Move',
        required=True, depends=['fiscalyear'],
        domain=[('period.fiscalyear', '=', Eval('fiscalyear', None))])

    @staticmethod
    def default_first_number():
        return 2


class RenumberSummaryMoves(Wizard):
    '''Renumber Summary Account Moves'''
    __name__ = 'account.summary.move.renumber'

    start = StateView('account.summary.move.renumber.start',
        'account_move_summary.summary_move_renumber_start_view_form',
            [
                Button('Cancel', 'end', 'tryton-cancel'),
                Button('Renumber', 'renumber', 'tryton-ok', default=True),
            ])
    renumber = StateAction('account_move_summary.act_summary_move_form')

    @classmethod
    def __setup__(cls):
        super().__setup__()
        cls._error_messages.update({
            'draft_moves_in_fiscalyear':
                'There are Draft Moves in Fiscal Year "%(fiscalyear)s".',
            })

    def do_renumber(self, action):
        pool = Pool()
        SummaryMove = pool.get('account.summary.move')
        Sequence = pool.get('ir.sequence')
        Warning = pool.get('res.user.warning')
        draft_moves = SummaryMove.search([
                ('period.fiscalyear', '=', self.start.fiscalyear.id),
                ('state', '=', 'draft'),
                ])
        if draft_moves:
            key = 'move_renumber_draft_moves%s' % self.start.fiscalyear.id
            if Warning.check(key):
                self.raise_user_error('draft_moves_in_fiscalyear', {
                        'fiscalyear': self.start.fiscalyear.rec_name,
                        })

        sequences = set([self.start.fiscalyear.post_summary_move_sequence])
        for period in self.start.fiscalyear.periods:
            if period.post_summary_move_sequence:
                sequences.add(period.post_summary_move_sequence)

        Sequence.write(list(sequences), {
                'number_next': self.start.first_number,
                })

        moves_to_renumber = SummaryMove.search([
                ('period.fiscalyear', '=', self.start.fiscalyear.id),
                ('post_number', '!=', None),
                ],
            order=[
                ('date', 'ASC'),
                ('id', 'ASC'),
                ])
        move_vals = []
        for move in moves_to_renumber:
            if move == self.start.first_move:
                number_next_old = \
                    move.period.post_summary_move_sequence_used.number_next
                Sequence.write(list(sequences), {
                        'number_next': 1,
                        })
                move_vals.extend(([move], {
                        'post_number': Sequence.get_id(
                            move.period.post_summary_move_sequence_used.id),
                        }))
                Sequence.write(list(sequences), {
                        'number_next': number_next_old,
                        })
                continue
            move_vals.extend(([move], {
                        'post_number': Sequence.get_id(
                            move.period.post_summary_move_sequence_used.id),
                        }))
        SummaryMove.write(*move_vals)

        action['pyson_domain'] = PYSONEncoder().encode([
            ('period.fiscalyear', '=', self.start.fiscalyear.id),
            ('post_number', '!=', None),
            ])
        return action, {}

    def transition_renumber(self):
        return 'end'


class SummaryGeneralJournal(Report):
    __name__ = 'account.summary.move.general_journal'

    @classmethod
    def get_context(cls, records, data):
        pool = Pool()
        Company = pool.get('company.company')
        records = sorted(records, key=lambda i: (i.post_number, i.date))
        context = Transaction().context
        report_context = super(SummaryGeneralJournal, cls).get_context(
            records, data)
        report_context['company'] = Company(context['company'])
        report_context['get_total_move'] = cls.get_total_move
        return report_context

    @classmethod
    def get_total_move(self, lines, type_):
        if type_ == 'debit':
            return reduce(lambda a, b: a + b, [l.debit for l in lines])
        elif type_ == 'credit':
            return reduce(lambda a, b: a + b, [l.credit for l in lines])
