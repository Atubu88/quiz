"""SQLAlchemy ORM models for the FastAPI web application database.

These models are scoped exclusively to the webapp project so that the
Telegram bot can continue working with the existing cloud database.
"""
from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import declarative_base, relationship
from sqlalchemy.sql import func


Base = declarative_base()
metadata = Base.metadata


class Category(Base):
    __tablename__ = "categories"

    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    is_active = Column(Boolean, nullable=False, server_default=text("true"))
    description = Column(Text)

    quizzes = relationship("Quiz", back_populates="category")


class MatchingQuiz(Base):
    __tablename__ = "matching_quizzes"

    id = Column(Integer, primary_key=True)
    title = Column(Text, nullable=False)
    difficulty = Column(Text)
    pairs = Column(JSONB, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    telegraph_url = Column(Text)

    results = relationship("MatchingQuizResult", back_populates="quiz", cascade="all, delete-orphan")


class MatchingQuizResult(Base):
    __tablename__ = "matching_quiz_results"

    id = Column(Integer, primary_key=True)
    quiz_id = Column(Integer, ForeignKey("matching_quizzes.id", ondelete="CASCADE"), nullable=False)
    user_id = Column(BigInteger, ForeignKey("users.telegram_id", ondelete="CASCADE"), nullable=False)
    is_correct = Column(Boolean, nullable=False, server_default=text("false"))
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    error_count = Column(Integer, server_default=text("0"))
    time_taken = Column(Float)

    quiz = relationship("MatchingQuiz", back_populates="results")
    user = relationship(
        "User",
        back_populates="matching_quiz_results",
        primaryjoin="User.telegram_id==MatchingQuizResult.user_id",
        foreign_keys=[user_id],
    )


class Quiz(Base):
    __tablename__ = "quizzes"

    id = Column(Integer, primary_key=True)
    title = Column(String, nullable=False)
    is_active = Column(Boolean, server_default=text("true"))
    category_id = Column(Integer, ForeignKey("categories.id", ondelete="SET NULL"))
    description = Column(Text)

    category = relationship("Category", back_populates="quizzes")
    questions = relationship("Question", back_populates="quiz", cascade="all, delete-orphan")
    quiz_results = relationship("QuizResult", back_populates="quiz", cascade="all, delete-orphan")
    results = relationship("Result", back_populates="quiz", cascade="all, delete-orphan")
    user_attempts = relationship("UserAttempt", back_populates="quiz", cascade="all, delete-orphan")
    team_results = relationship("TeamResult", back_populates="quiz", cascade="all, delete-orphan")


class Question(Base):
    __tablename__ = "questions"

    id = Column(Integer, primary_key=True)
    text = Column(Text, nullable=False)
    explanation = Column(Text)
    quiz_id = Column(Integer, ForeignKey("quizzes.id", ondelete="CASCADE"))

    quiz = relationship("Quiz", back_populates="questions")
    options = relationship("Option", back_populates="question", cascade="all, delete-orphan")


class Option(Base):
    __tablename__ = "options"

    id = Column(Integer, primary_key=True)
    text = Column(Text, nullable=False)
    is_correct = Column(Boolean)
    question_id = Column(Integer, ForeignKey("questions.id", ondelete="CASCADE"))

    question = relationship("Question", back_populates="options")


class PollQuizQuestion(Base):
    __tablename__ = "poll_quiz_questions"

    id = Column(Integer, primary_key=True)
    question = Column(Text, nullable=False)
    options = Column(ARRAY(Text), nullable=False)
    correct_answer = Column(Integer, nullable=False)
    explanation = Column(Text)
    theme = Column(Text)


class PollQuizResult(Base):
    __tablename__ = "poll_quiz_results"

    id = Column(Integer, primary_key=True)
    user_id = Column(BigInteger, ForeignKey("users.telegram_id", ondelete="CASCADE"), nullable=False)
    username = Column(Text)
    score = Column(Integer, server_default=text("0"))
    time_spent = Column(Integer, server_default=text("0"))

    user = relationship(
        "User",
        back_populates="poll_quiz_results",
        primaryjoin="User.telegram_id==PollQuizResult.user_id",
        foreign_keys=[user_id],
    )


class QuizResult(Base):
    __tablename__ = "quiz_results"

    id = Column(Integer, primary_key=True)
    user_id = Column(BigInteger, ForeignKey("users.telegram_id", ondelete="CASCADE"))
    quiz_id = Column(Integer, ForeignKey("quizzes.id", ondelete="CASCADE"))
    is_correct = Column(Boolean, nullable=False)
    created_at = Column(DateTime(timezone=False), server_default=func.now())
    time_taken = Column(Float)

    quiz = relationship("Quiz", back_populates="quiz_results")
    user = relationship(
        "User",
        back_populates="quiz_results",
        primaryjoin="User.telegram_id==QuizResult.user_id",
        foreign_keys=[user_id],
    )


class QuizNew(Base):
    __tablename__ = "quizzes_new"

    id = Column(Integer, primary_key=True)
    title = Column(Text, nullable=False)
    correct_order = Column(ARRAY(Text), nullable=False)
    created_at = Column(DateTime(timezone=False), server_default=func.now())
    difficulty = Column(Text, server_default=text("'не указана'"))
    extra_link = Column(Text)


class Result(Base):
    __tablename__ = "results"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"))
    quiz_id = Column(Integer, ForeignKey("quizzes.id", ondelete="SET NULL"))
    score = Column(Integer)
    time_taken = Column(Float)
    date_taken = Column(DateTime(timezone=False), server_default=func.now())

    user = relationship("User", back_populates="results")
    quiz = relationship("Quiz", back_populates="results")


class SelfReportTest(Base):
    __tablename__ = "self_report_tests"

    id = Column(Integer, primary_key=True)
    title = Column(String, nullable=False)
    description = Column(Text)
    questions = Column(JSONB, nullable=False)
    results = Column(JSONB)


class Setting(Base):
    __tablename__ = "settings"

    id = Column(Integer, primary_key=True)
    is_timer_enabled = Column(Boolean)


class SurvivalResult(Base):
    __tablename__ = "survival_results"

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    user_id = Column(BigInteger, ForeignKey("users.telegram_id", ondelete="CASCADE"), nullable=False)
    username = Column(Text, nullable=False)
    score = Column(Integer, nullable=False, server_default=text("0"))
    time_spent = Column(Integer, nullable=False, server_default=text("0"))
    created_at = Column(DateTime(timezone=False), server_default=func.now())

    user = relationship(
        "User",
        back_populates="survival_results",
        primaryjoin="User.telegram_id==SurvivalResult.user_id",
        foreign_keys=[user_id],
    )


class Team(Base):
    __tablename__ = "teams"

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    name = Column(Text, nullable=False)
    code = Column(Text, nullable=False)
    captain_id = Column(BigInteger, ForeignKey("users.telegram_id"), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    start_time = Column(DateTime(timezone=True))
    match_id = Column(Text)
    ready = Column(Boolean, server_default=text("false"))
    quiz_id = Column(Text)

    captain = relationship(
        "User",
        primaryjoin="User.telegram_id==Team.captain_id",
        viewonly=True,
        foreign_keys=[captain_id],
    )
    members = relationship("TeamMember", back_populates="team", cascade="all, delete-orphan")
    results = relationship("TeamResult", back_populates="team", cascade="all, delete-orphan")


class TeamMember(Base):
    __tablename__ = "team_members"

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    team_id = Column(UUID(as_uuid=True), ForeignKey("teams.id", ondelete="CASCADE"))
    user_id = Column(BigInteger, ForeignKey("users.telegram_id", ondelete="CASCADE"))
    is_captain = Column(Boolean, server_default=text("false"))
    joined_at = Column(DateTime(timezone=True), server_default=func.now())

    team = relationship("Team", back_populates="members")
    user = relationship(
        "User",
        back_populates="team_memberships",
        primaryjoin="User.telegram_id==TeamMember.user_id",
        foreign_keys=[user_id],
    )


class TeamResult(Base):
    __tablename__ = "team_results"

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    team_id = Column(UUID(as_uuid=True), ForeignKey("teams.id", ondelete="CASCADE"))
    quiz_id = Column(Integer, ForeignKey("quizzes.id", ondelete="SET NULL"))
    score = Column(Integer, nullable=False)
    time_taken = Column(Float)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    team = relationship("Team", back_populates="results")
    quiz = relationship("Quiz", back_populates="team_results")


class UserAttempt(Base):
    __tablename__ = "user_attempts"

    id = Column(Integer, primary_key=True)
    user_id = Column(BigInteger, ForeignKey("users.telegram_id", ondelete="CASCADE"), nullable=False)
    quiz_id = Column(Integer, ForeignKey("quizzes.id", ondelete="CASCADE"))
    selected_count = Column(Integer, server_default=text("0"))
    created_at = Column(DateTime(timezone=False), server_default=func.now())

    quiz = relationship("Quiz", back_populates="user_attempts")
    user = relationship(
        "User",
        back_populates="user_attempts",
        primaryjoin="User.telegram_id==UserAttempt.user_id",
        foreign_keys=[user_id],
    )


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    telegram_id = Column(BigInteger, nullable=False, unique=True)
    username = Column(String)
    first_name = Column(String)
    last_name = Column(String)

    results = relationship("Result", back_populates="user", cascade="all, delete-orphan")
    quiz_results = relationship("QuizResult", back_populates="user", cascade="all, delete-orphan")
    poll_quiz_results = relationship("PollQuizResult", back_populates="user", cascade="all, delete-orphan")
    matching_quiz_results = relationship("MatchingQuizResult", back_populates="user", cascade="all, delete-orphan")
    survival_results = relationship("SurvivalResult", back_populates="user", cascade="all, delete-orphan")
    team_memberships = relationship("TeamMember", back_populates="user", cascade="all, delete-orphan")
    user_attempts = relationship("UserAttempt", back_populates="user", cascade="all, delete-orphan")


__all__ = (
    "Base",
    "metadata",
    "Category",
    "MatchingQuiz",
    "MatchingQuizResult",
    "Quiz",
    "Question",
    "Option",
    "PollQuizQuestion",
    "PollQuizResult",
    "QuizResult",
    "QuizNew",
    "Result",
    "SelfReportTest",
    "Setting",
    "SurvivalResult",
    "Team",
    "TeamMember",
    "TeamResult",
    "UserAttempt",
    "User",
)
