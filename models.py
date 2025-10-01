from sqlalchemy import (
    Column,
    Integer,
    String,
    Text,
    Boolean,
    ForeignKey,
    DateTime,
    Float,
    BigInteger,
    JSON,
    Index
)
from sqlalchemy.orm import relationship, declarative_base
from sqlalchemy.sql import func

Base = declarative_base()

class User(Base):
    __tablename__ = 'users'
    __table_args__ = (
        Index('ix_users_telegram_id', 'telegram_id'),
    )

    id = Column(Integer, primary_key=True)
    telegram_id = Column(BigInteger, unique=True, nullable=False)
    username = Column(String(255))
    first_name = Column(String(255))
    last_name = Column(String(255))

    results = relationship('Result', back_populates='user')


class Quiz(Base):
    __tablename__ = 'quizzes'

    id = Column(Integer, primary_key=True)
    title = Column(String(255), nullable=False)

    # При удалении викторины будут удаляться связанные вопросы и результаты
    questions = relationship(
        'Question',
        back_populates='quiz',
        cascade="all, delete-orphan"
    )

    results = relationship(
        'Result',
        back_populates='quiz',
        cascade="all, delete-orphan"
    )


class Question(Base):
    __tablename__ = 'questions'
    __table_args__ = (
        Index('ix_questions_quiz_id', 'quiz_id'),
    )

    id = Column(Integer, primary_key=True)
    text = Column(Text, nullable=False)
    explanation = Column(Text, nullable=True)

    quiz_id = Column(Integer, ForeignKey('quizzes.id'))
    quiz = relationship('Quiz', back_populates='questions')

    options = relationship(
        'Option',
        back_populates='question',
        cascade="all, delete-orphan"
    )


class Option(Base):
    __tablename__ = 'options'
    __table_args__ = (
        Index('ix_options_question_id', 'question_id'),
    )

    id = Column(Integer, primary_key=True)
    text = Column(Text, nullable=False)
    is_correct = Column(Boolean, default=False)

    question_id = Column(Integer, ForeignKey('questions.id'))
    question = relationship('Question', back_populates='options')


class Result(Base):
    __tablename__ = 'results'
    __table_args__ = (
        Index('ix_results_user_id', 'user_id'),
        Index('ix_results_quiz_id', 'quiz_id'),
    )

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'))
    quiz_id = Column(Integer, ForeignKey('quizzes.id'))
    score = Column(Integer)
    time_taken = Column(Float)  # Время в секундах
    date_taken = Column(DateTime, default=func.now())

    user = relationship('User', back_populates='results')
    quiz = relationship('Quiz', back_populates='results')


class Settings(Base):
    __tablename__ = 'settings'

    id = Column(Integer, primary_key=True)
    is_timer_enabled = Column(Boolean, default=True)


class SelfReportTest(Base):
    __tablename__ = 'self_report_tests'

    id = Column(Integer, primary_key=True)
    title = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    questions = Column(JSON, nullable=False)  # Вопросы и ответы в формате JSON
    results = Column(JSON, nullable=True)      # Рекомендации по итогам теста
