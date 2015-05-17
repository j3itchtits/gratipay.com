from __future__ import absolute_import, division, print_function, unicode_literals

from decimal import Decimal as D
import os

import balanced
import mock
import pytest

from gratipay.billing.exchanges import create_card_hold
from gratipay.billing.payday import NoPayday, Payday
from gratipay.exceptions import NegativeBalance
from gratipay.models.participant import Participant
from gratipay.testing import Foobar, Harness
from gratipay.testing.billing import BillingHarness
from gratipay.testing.emails import EmailHarness


class TestPayday(BillingHarness):

    @mock.patch.object(Payday, 'fetch_card_holds')
    def test_payday_moves_money(self, fch):
        A = self.make_team(is_approved=True)
        self.janet.set_subscription_to(A, '6.00')  # under $10!
        fch.return_value = {}
        Payday.start().run()

        janet = Participant.from_username('janet')
        hannibal = Participant.from_username('hannibal')

        assert hannibal.balance == D('6.00')
        assert janet.balance == D('3.41')

    @mock.patch.object(Payday, 'fetch_card_holds')
    def test_payday_doesnt_move_money_from_a_suspicious_account(self, fch):
        self.db.run("""
            UPDATE participants
               SET is_suspicious = true
             WHERE username = 'janet'
        """)
        team = self.make_team(owner=self.homer, is_approved=True)
        self.janet.set_subscription_to(team, '6.00')  # under $10!
        fch.return_value = {}
        Payday.start().run()

        janet = Participant.from_username('janet')
        homer = Participant.from_username('homer')

        assert janet.balance == D('0.00')
        assert homer.balance == D('0.00')

    @mock.patch.object(Payday, 'fetch_card_holds')
    def test_payday_doesnt_move_money_to_a_suspicious_account(self, fch):
        self.db.run("""
            UPDATE participants
               SET is_suspicious = true
             WHERE username = 'homer'
        """)
        team = self.make_team(owner=self.homer, is_approved=True)
        self.janet.set_subscription_to(team, '6.00')  # under $10!
        fch.return_value = {}
        Payday.start().run()

        janet = Participant.from_username('janet')
        homer = Participant.from_username('homer')

        assert janet.balance == D('0.00')
        assert homer.balance == D('0.00')

    @mock.patch.object(Payday, 'fetch_card_holds')
    def test_payday_moves_money_with_balanced(self, fch):
        team = self.make_team(owner=self.homer, is_approved=True)
        self.janet.set_subscription_to(team, '15.00')
        fch.return_value = {}
        Payday.start().run()

        janet = Participant.from_username('janet')
        homer = Participant.from_username('homer')

        assert janet.balance == D('0.00')
        assert homer.balance == D('0.00')

        janet_customer = balanced.Customer.fetch(janet.balanced_customer_href)
        homer_customer = balanced.Customer.fetch(homer.balanced_customer_href)

        created_at = balanced.Transaction.f.created_at

        credit = homer_customer.credits.sort(created_at.desc()).first()
        assert credit.amount == 1500
        assert credit.description == 'homer'

        debit = janet_customer.debits.sort(created_at.desc()).first()
        assert debit.amount == 1576  # base amount + fee
        assert debit.description == 'janet'

    @mock.patch.object(Payday, 'fetch_card_holds')
    @mock.patch('gratipay.billing.payday.create_card_hold')
    def test_ncc_failing(self, cch, fch):
        team = self.make_team('gratiteam', owner=self.homer, is_approved=True)
        self.janet.set_subscription_to(team, 24)
        fch.return_value = {}
        cch.return_value = (None, 'oops')
        payday = Payday.start()
        before = self.fetch_payday()
        assert before['ncc_failing'] == 0
        payday.payin()
        after = self.fetch_payday()
        assert after['ncc_failing'] == 1

    def test_update_cached_amounts(self):
        team = self.make_participant('team', claimed_time='now', number='plural')
        alice = self.make_participant('alice', claimed_time='now', last_bill_result='')
        bob = self.make_participant('bob', claimed_time='now')
        carl = self.make_participant('carl', claimed_time='now', last_bill_result="Fail!")
        dana = self.make_participant('dana', claimed_time='now')
        emma = self.make_participant('emma')
        alice.set_tip_to(dana, '3.00')
        alice.set_tip_to(bob, '6.00')
        alice.set_tip_to(emma, '1.00')
        alice.set_tip_to(team, '4.00')
        bob.set_tip_to(alice, '5.00')
        team.add_member(bob)
        team.set_take_for(bob, D('1.00'), bob)
        bob.set_tip_to(dana, '2.00')  # funded by bob's take
        bob.set_tip_to(emma, '7.00')  # not funded, insufficient receiving
        carl.set_tip_to(dana, '2.08')  # not funded, failing card

        def check():
            alice = Participant.from_username('alice')
            bob = Participant.from_username('bob')
            carl = Participant.from_username('carl')
            dana = Participant.from_username('dana')
            emma = Participant.from_username('emma')
            assert alice.giving == D('13.00')
            assert alice.receiving == D('5.00')
            assert bob.giving == D('7.00')
            assert bob.receiving == D('7.00')
            assert bob.taking == D('1.00')
            assert carl.giving == D('0.00')
            assert carl.receiving == D('0.00')
            assert dana.receiving == D('5.00')
            assert dana.npatrons == 2
            assert emma.receiving == D('1.00')
            assert emma.npatrons == 1
            funded_tips = self.db.all("SELECT amount FROM tips WHERE is_funded ORDER BY id")
            assert funded_tips == [3, 6, 1, 4, 5, 2]

        # Pre-test check
        check()

        # Check that update_cached_amounts doesn't mess anything up
        Payday.start().update_cached_amounts()
        check()

        # Check that update_cached_amounts actually updates amounts
        self.db.run("""
            UPDATE tips SET is_funded = false;
            UPDATE participants
               SET giving = 0
                 , npatrons = 0
                 , receiving = 0
                 , taking = 0;
        """)
        Payday.start().update_cached_amounts()
        check()

    def test_update_cached_amounts_depth(self):
        alice = self.make_participant('alice', claimed_time='now', last_bill_result='')
        usernames = ('bob', 'carl', 'dana', 'emma', 'fred', 'greg')
        users = [self.make_participant(username, claimed_time='now') for username in usernames]

        prev = alice
        for user in reversed(users):
            prev.set_tip_to(user, '1.00')
            prev = user

        def check():
            for username in reversed(usernames[1:]):
                user = Participant.from_username(username)
                assert user.giving == D('1.00')
                assert user.receiving == D('1.00')
                assert user.npatrons == 1
            funded_tips = self.db.all("SELECT id FROM tips WHERE is_funded ORDER BY id")
            assert len(funded_tips) == 6

        check()
        Payday.start().update_cached_amounts()
        check()

    @mock.patch('gratipay.billing.payday.log')
    def test_start_prepare(self, log):
        self.clear_tables()
        self.make_participant('bob', balance=10, claimed_time=None)
        self.make_participant('carl', balance=10, claimed_time='now')

        payday = Payday.start()
        ts_start = payday.ts_start

        get_participants = lambda c: c.all("SELECT * FROM payday_participants")

        with self.db.get_cursor() as cursor:
            payday.prepare(cursor, ts_start)
            participants = get_participants(cursor)

        expected_logging_call_args = [
            ('Starting a new payday.'),
            ('Payday started at {}.'.format(ts_start)),
            ('Prepared the DB.'),
        ]
        expected_logging_call_args.reverse()
        for args, _ in log.call_args_list:
            assert args[0] == expected_logging_call_args.pop()

        log.reset_mock()

        # run a second time, we should see it pick up the existing payday
        payday = Payday.start()
        second_ts_start = payday.ts_start
        with self.db.get_cursor() as cursor:
            payday.prepare(cursor, second_ts_start)
            second_participants = get_participants(cursor)

        assert ts_start == second_ts_start
        participants = list(participants)
        second_participants = list(second_participants)

        # carl is the only valid participant as he has a claimed time
        assert len(participants) == 1
        assert participants == second_participants

        expected_logging_call_args = [
            ('Picking up with an existing payday.'),
            ('Payday started at {}.'.format(second_ts_start)),
            ('Prepared the DB.'),
        ]
        expected_logging_call_args.reverse()
        for args, _ in log.call_args_list:
            assert args[0] == expected_logging_call_args.pop()

    def test_end(self):
        Payday.start().end()
        result = self.db.one("SELECT count(*) FROM paydays "
                             "WHERE ts_end > '1970-01-01'")
        assert result == 1

    def test_end_raises_NoPayday(self):
        with self.assertRaises(NoPayday):
            Payday().end()

    @mock.patch('gratipay.billing.payday.log')
    @mock.patch('gratipay.billing.payday.Payday.payin')
    def test_payday(self, payin, log):
        greeting = 'Greetings, program! It\'s PAYDAY!!!!'
        Payday.start().run()
        log.assert_any_call(greeting)
        assert payin.call_count == 1


class TestPayin(BillingHarness):

    def create_card_holds(self):
        payday = Payday.start()
        with self.db.get_cursor() as cursor:
            payday.prepare(cursor, payday.ts_start)
            return payday.create_card_holds(cursor)

    @mock.patch.object(Payday, 'fetch_card_holds')
    @mock.patch('gratipay.billing.payday.create_card_hold')
    def test_hold_amount_includes_negative_balance(self, cch, fch):
        self.db.run("""
            UPDATE participants SET balance = -10 WHERE username='janet'
        """)
        team = self.make_team('The A Team', is_approved=True)
        self.janet.set_subscription_to(team, 25)
        fch.return_value = {}
        cch.return_value = (None, 'some error')
        self.create_card_holds()
        assert cch.call_args[0][-1] == 35

    def test_payin_fetches_and_uses_existing_holds(self):
        team = self.make_team(owner=self.homer, is_approved=True)
        self.janet.set_subscription_to(team, '20.00')
        hold, error = create_card_hold(self.db, self.janet, D(20))
        assert hold is not None
        assert not error
        with mock.patch('gratipay.billing.payday.create_card_hold') as cch:
            cch.return_value = (None, None)
            self.create_card_holds()
            assert not cch.called, cch.call_args_list

    @mock.patch.object(Payday, 'fetch_card_holds')
    def test_payin_cancels_existing_holds_of_insufficient_amounts(self, fch):
        team = self.make_team(owner=self.homer, is_approved=True)
        self.janet.set_subscription_to(team, '30.00')
        hold, error = create_card_hold(self.db, self.janet, D(10))
        assert not error
        fch.return_value = {self.janet.id: hold}
        with mock.patch('gratipay.billing.payday.create_card_hold') as cch:
            fake_hold = object()
            cch.return_value = (fake_hold, None)
            holds = self.create_card_holds()
            assert len(holds) == 1
            assert holds[self.janet.id] is fake_hold
            assert hold.voided_at is not None

    @mock.patch('gratipay.billing.payday.CardHold')
    @mock.patch('gratipay.billing.payday.cancel_card_hold')
    def test_fetch_card_holds_handles_extra_holds(self, cancel, CardHold):
        fake_hold = mock.MagicMock()
        fake_hold.meta = {'participant_id': 0}
        fake_hold.amount = 1061
        fake_hold.save = mock.MagicMock()
        CardHold.query.filter.return_value = [fake_hold]
        for attr, state in (('failure_reason', 'failed'),
                            ('voided_at', 'cancelled'),
                            ('debit_href', 'captured')):
            holds = Payday.fetch_card_holds(set())
            assert fake_hold.meta['state'] == state
            fake_hold.save.assert_called_with()
            assert len(holds) == 0
            setattr(fake_hold, attr, None)
        holds = Payday.fetch_card_holds(set())
        cancel.assert_called_with(fake_hold)
        assert len(holds) == 0

    @mock.patch('gratipay.billing.payday.log')
    def test_payin_cancels_uncaptured_holds(self, log):
        team = self.make_team(owner=self.homer, is_approved=True)
        self.janet.set_subscription_to(team, '20.00')
        team.add_member(self.janet)
        # Manually hike janet's take to $20
        self.db.run("""
            UPDATE payroll
               SET amount='20'
             WHERE team=%s
               AND member=%s
        """, (team.slug, self.janet.username))
        Payday.start().payin()

        assert log.call_args_list[-6][0] == ("Captured 0 card holds.",)
        assert log.call_args_list[-4][0] == ("Canceled 1 card holds.",)
        assert Participant.from_id(self.janet.id).balance == 0
        assert Participant.from_id(self.homer.id).balance == 0

    def test_payin_cant_make_balances_more_negative(self):
        self.db.run("""
            UPDATE participants SET balance = -10 WHERE username='janet'
        """)
        payday = Payday.start()
        with self.db.get_cursor() as cursor:
            payday.prepare(cursor, payday.ts_start)
            cursor.run("""
                UPDATE payday_participants
                   SET new_balance = -50
                 WHERE username IN ('janet', 'homer')
            """)
            with self.assertRaises(NegativeBalance):
                payday.update_balances(cursor)

    @mock.patch.object(Payday, 'fetch_card_holds')
    @mock.patch('gratipay.billing.exchanges.thing_from_href')
    def test_card_hold_error(self, tfh, fch):
        team = self.make_team(owner=self.homer, is_approved=True)
        self.janet.set_subscription_to(team, '17.00')
        tfh.side_effect = Foobar
        fch.return_value = {}
        Payday.start().payin()
        payday = self.fetch_payday()
        assert payday['ncc_failing'] == 1

    def test_payin_doesnt_make_null_payments(self):
        team = self.make_team('Gratiteam', is_approved=True)
        alice = self.make_participant('alice', claimed_time='now')
        alice.set_subscription_to(team, 1)
        alice.set_subscription_to(team, 0)
        a_team = self.make_participant('a_team', claimed_time='now', number='plural')
        a_team.add_member(alice)
        Payday.start().payin()
        payments = self.db.all("SELECT * FROM payments WHERE amount = 0")
        assert not payments

    def test_process_subscriptions(self):
        alice = self.make_participant('alice', claimed_time='now', balance=1)
        hannibal = self.make_participant('hannibal', claimed_time='now', last_ach_result='')
        lecter = self.make_participant('lecter', claimed_time='now', last_ach_result='')
        A = self.make_team('The A Team', hannibal, is_approved=True)
        B = self.make_team('The B Team', lecter, is_approved=True)
        alice.set_subscription_to(A, D('0.51'))
        alice.set_subscription_to(B, D('0.50'))

        payday = Payday.start()
        with self.db.get_cursor() as cursor:
            payday.prepare(cursor, payday.ts_start)
            payday.process_subscriptions(cursor)
            assert cursor.one("select balance from payday_teams where slug='TheATeam'") == D('0.51')
            assert cursor.one("select balance from payday_teams where slug='TheBTeam'") == 0
            payday.update_balances(cursor)

        assert Participant.from_id(alice.id).balance == D('0.49')
        assert Participant.from_username('hannibal').balance == 0
        assert Participant.from_username('lecter').balance == 0

        payment = self.db.one("SELECT * FROM payments")
        assert payment.amount == D('0.51')
        assert payment.direction == 'to-team'

    def test_process_payroll(self):
        team_owner = self.make_participant('team_owner', claimed_time='now', last_ach_result='')
        team_funder = self.make_participant('team_funder', claimed_time='now', balance=50)
        team = self.make_team(is_approved=True, owner=team_owner)
        team_funder.set_subscription_to(team, D('20.00'))

        alice = self.make_participant('alice', claimed_time='now')
        team.add_member(alice)
        team.add_member(self.make_participant('bob', claimed_time='now'))
        team.set_take_for(alice, D('1.00'), alice)

        payday = Payday.start()

        # Test that payday ignores takes set after it started
        team.set_take_for(alice, D('2.00'), alice)

        # Run the transfer multiple times to make sure we ignore takes that
        # have already been processed
        for i in range(3):
            with self.db.get_cursor() as cursor:
                payday.prepare(cursor, payday.ts_start)
                payday.process_subscriptions(cursor)
                payday.process_payroll(cursor, payday.ts_start)
                payday.process_draws(cursor)
                payday.update_balances(cursor)

        participants = self.db.all("SELECT username, balance FROM participants")

        for p in participants:
            if p.username == 'team_owner':
                assert p.balance == D('18.99')
            elif p.username == 'alice':
                assert p.balance == D('1.00')
            elif p.username == 'bob':
                assert p.balance == D('0.01')
            elif p.username == 'team_funder':
                assert p.balance == D('30.00')
            else:
                assert p.balance == 0

    def test_process_draws(self):
        alice = self.make_participant('alice', claimed_time='now', balance=1)
        hannibal = self.make_participant('hannibal', claimed_time='now', last_ach_result='')
        A = self.make_team('The A Team', hannibal, is_approved=True)
        alice.set_subscription_to(A, D('0.51'))

        payday = Payday.start()
        with self.db.get_cursor() as cursor:
            payday.prepare(cursor, payday.ts_start)
            payday.process_subscriptions(cursor)
            payday.process_payroll(cursor, payday.ts_start)
            payday.process_draws(cursor)
            assert cursor.one("select new_balance from payday_participants "
                              "where username='hannibal'") == D('0.51')
            assert cursor.one("select balance from payday_teams where slug='TheATeam'") == 0
            payday.update_balances(cursor)

        assert Participant.from_id(alice.id).balance == D('0.49')
        assert Participant.from_username('hannibal').balance == D('0.51')

        payment = self.db.one("SELECT * FROM payments WHERE direction='to-participant'")
        assert payment.amount == D('0.51')

    @mock.patch.object(Payday, 'fetch_card_holds')
    def test_transfer_takes_doesnt_make_negative_transfers(self, fch):
        hold = balanced.CardHold(amount=1500, meta={'participant_id': self.janet.id},
                                 card_href=self.card_href)
        hold.capture = lambda *a, **kw: None
        hold.save = lambda *a, **kw: None
        fch.return_value = {self.janet.id: hold}

        team = self.make_team('Gratiteam', self.homer, is_approved=True)
        self.janet.set_subscription_to(team, D('0.5')) # Total team funds is $0.5
        bob = self.make_participant('bob', claimed_time='now')
        team.add_member(bob) # Bob should receive $0, coz the $0.5 is exhausted (kids first)
        team.add_member(self.david)
        team.set_take_for(self.david, D('1.00'), self.david) # David should only receive $0.5

        Payday.start().payin()

        assert Participant.from_id(bob.id).balance == 0
        assert Participant.from_id(self.homer.id).balance == 0
        assert Participant.from_id(self.david.id).balance == D('0.50')
        assert Participant.from_id(self.janet.id).balance == D('8.91') # Upcharged

    def test_take_over_during_payin(self):
        alice = self.make_participant('alice', claimed_time='now', balance=50)
        bob = self.make_participant('bob', claimed_time='now', elsewhere='twitter', last_ach_result='')
        team = self.make_team('team', owner=bob, is_approved=True)
        alice.set_subscription_to(team, D('18.00'))
        payday = Payday.start()
        with self.db.get_cursor() as cursor:
            payday.prepare(cursor, payday.ts_start)
            bruce = self.make_participant('bruce', claimed_time='now')
            bruce.take_over(('twitter', str(bob.id)), have_confirmation=True)
            payday.process_subscriptions(cursor)
            bruce.delete_elsewhere('twitter', str(bob.id))
            billy = self.make_participant('billy', claimed_time='now')
            billy.take_over(('github', str(bruce.id)), have_confirmation=True)
            payday.process_payroll(cursor, payday.ts_start)
            payday.process_draws(cursor)
            payday.update_balances(cursor)
        payday.take_over_balances()
        assert Participant.from_id(bob.id).balance == 0
        assert Participant.from_id(bruce.id).balance == 0
        assert Participant.from_id(billy.id).balance == 18

    @mock.patch.object(Payday, 'fetch_card_holds')
    @mock.patch('gratipay.billing.payday.capture_card_hold')
    def test_payin_dumps_transfers_for_debugging(self, cch, fch):
        team = self.make_team(owner=self.homer, is_approved=True)
        self.janet.set_subscription_to(team, '10.00')
        fake_hold = mock.MagicMock()
        fake_hold.amount = 1500
        fch.return_value = {self.janet.id: fake_hold}
        cch.side_effect = Foobar
        open_ = mock.MagicMock()
        open_.side_effect = open
        with mock.patch.dict(__builtins__, {'open': open_}):
            with self.assertRaises(Foobar):
                Payday.start().payin()
        filename = open_.call_args_list[-1][0][0]
        assert filename.endswith('_payments.csv')
        os.unlink(filename)


class TestPayout(Harness):

    def test_payout_no_balanced_href_does_________what_question_mark(self):
        self.make_participant('alice', claimed_time='now', is_suspicious=False,
                              balance=20)
        Payday.start().payout()

    @mock.patch('gratipay.billing.payday.ach_credit')
    def test_payout_can_pay_out(self, ach):
        alice = self.make_participant('alice', claimed_time='now', is_suspicious=False,
                              balanced_customer_href='foo',
                              last_ach_result='')
        self.make_exchange('balanced-cc', 20, 0, alice)
        self.make_team(owner='alice', is_approved=True)
        Payday.start().payout()

        assert ach.call_count == 1
        assert ach.call_args_list[0][0][1].id == alice.id
        assert ach.call_args_list[0][0][2] == 0

    @mock.patch('gratipay.billing.payday.log')
    def test_payout_skips_unreviewed(self, log):
        self.make_participant('alice', claimed_time='now', is_suspicious=None,
                              balance=20, balanced_customer_href='foo',
                              last_ach_result='')
        self.make_team(owner='alice', is_approved=True)
        Payday.start().payout()
        log.assert_any_call('UNREVIEWED: alice')

    @mock.patch('gratipay.billing.payday.ach_credit')
    def test_payout_ach_error_gets_recorded(self, ach_credit):
        self.make_participant('alice', claimed_time='now', is_suspicious=False,
                              balance=20, balanced_customer_href='foo',
                              last_ach_result='')
        self.make_team(owner='alice', is_approved=True)
        ach_credit.return_value = 'some error'
        Payday.start().payout()
        payday = self.fetch_payday()
        assert payday['nach_failing'] == 1

    @mock.patch('gratipay.billing.payday.ach_credit')
    def test_payout_is_constrained_to_owners_and_payroll(self, ach):
        team_owner = self.make_participant('team_owner', claimed_time='now',
                                           last_ach_result='', is_suspicious=False)
        team = self.make_team('gratiteam', owner=team_owner, is_approved=True)
        alice = self.make_participant('alice', claimed_time='now', is_suspicious=False,
                                      last_ach_result='')
        bob = self.make_participant('bob', claimed_time='now', is_suspicious=False,
                                      last_ach_result='')
        team.add_member(bob)
        self.make_exchange('balanced-cc', 20, 0, alice) # Alice shouldn't be paid out
        self.make_exchange('balanced-cc', 30, 0, bob)
        self.make_exchange('balanced-cc', 30, 0, team_owner)

        Payday.start().payout()

        assert ach.call_count == 2

        # Check for specifics



class TestNotifyParticipants(EmailHarness):

    def test_it_notifies_participants(self):
        kalel = self.make_participant('kalel', claimed_time='now', is_suspicious=False,
                                      email_address='kalel@example.net', notify_charge=3)
        lily = self.make_participant('lily', claimed_time='now', is_suspicious=False)
        kalel.set_tip_to(lily, 10)

        for status in ('failed', 'succeeded'):
            payday = Payday.start()
            self.make_exchange('balanced-cc', 10, 0, kalel, status)
            payday.end()
            payday.notify_participants()

            emails = self.db.one('SELECT * FROM email_queue')
            assert emails.spt_name == 'charge_'+status

            Participant.dequeue_emails()
            assert self.get_last_email()['to'][0]['email'] == 'kalel@example.net'
